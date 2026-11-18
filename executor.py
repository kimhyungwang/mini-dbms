# executor.py
# 파싱된 문장(Insert / Select)을 실제로 실행하는 부분
#
#   SQL 글자 ──(sql_parser)──▶ 문장 객체 ──(executor)──▶ storage(파일) ──▶ 결과
#
# [Phase 2] SELECT의 WHERE에 id 조건(=, <, <=, >, >=)이 있으면 B+ 트리 인덱스로 찾고,
#           없으면 파일을 처음부터 끝까지 읽음(선형 탐색)
#
# [Phase 3] 여러 스레드가 동시에 execute()를 불러도 안전하게, 테이블마다 읽기/쓰기 락을 둠
#           SELECT는 같은 테이블이어도 동시에, INSERT는 그 테이블에서 혼자만 (다른 테이블은 상관없음)
#
# 테이블 구조는 db/schema.json 에 미리 정의되어 있다고 가정 (CREATE TABLE 없음)
#   {"users": {"name": "TEXT", "age": "INT"}}
#   모든 테이블에는 id(INT) 컬럼이 자동으로 맨 앞에 붙고, INSERT 때 1, 2, 3 … 으로 자동 부여

import json
import os
import time
from dataclasses import dataclass, field

from rwlock import RWLock
from sql_parser import Insert, Select, SQLError, parse_script
from storage import Table

TYPES = {"INT", "TEXT"}


@dataclass
class Result:
    kind: str                                   # "select", "insert", "error"
    source: str = ""                            # 실행한 SQL 원문
    columns: list = field(default_factory=list) # SELECT 결과 컬럼 이름
    rows: list = field(default_factory=list)    # SELECT 결과 행 목록
    message: str = ""                           # INSERT 결과 / 오류 메시지
    plan: str = ""                              # [Phase 2] SELECT 실행 방식 (인덱스 / 선형 탐색)
    examined: int = 0                           # [Phase 2] 파일에서 실제로 읽은 행 수
    total_rows: int = 0                         # 조건에 맞는 전체 행 수 (max_rows로 잘라도 전체 개수)
    elapsed_ms: float = 0.0                     # [Phase 2] 실행 시간


class Database:
    def __init__(self, data_dir="db", use_index=True):
        self.data_dir = data_dir
        self.use_index = use_index              # False면 id 조건이 있어도 선형 탐색 (속도 비교용)
        schema_path = os.path.join(data_dir, "schema.json")
        if not os.path.exists(schema_path):
            raise SQLError(f"스키마 파일이 없어요: {schema_path}")
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)

        self.tables = {}
        for table_name, cols in schema.items():
            columns = []
            for col, typ in cols.items():
                if col == "id":
                    raise SQLError(f"{table_name}: id는 자동으로 만들어지는 컬럼이라 스키마에 적지 않아요")
                if typ not in TYPES:
                    raise SQLError(f"{table_name}.{col}: 지원하지 않는 타입 {typ} (INT, TEXT만 가능)")
                columns.append((col, typ))
            self.tables[table_name] = Table(data_dir, table_name, columns)
        self.locks = {name: RWLock() for name in self.tables}     # [Phase 3] 테이블별 읽기/쓰기 락

    # ── 입구: SQL 글자(문장 여러 개 가능) → 결과 목록 ──
    # 한 문장이 실패해도 나머지 문장은 계속 실행
    # use_index: 이번 실행에서만 인덱스 사용 여부를 바꿀 때 (None이면 기본값)
    # max_rows: 결과 행을 이만큼만 모음 (개수는 끝까지 셈). 화면처럼 다 보여줄 수 없을 때 사용
    def execute(self, sql, use_index=None, max_rows=None):
        use_index = self.use_index if use_index is None else use_index
        results = []
        for stmt, source in parse_script(sql):
            if isinstance(stmt, SQLError):
                results.append(Result("error", source, message=str(stmt)))
                continue
            try:
                start = time.perf_counter()
                result = self.run(stmt, use_index, max_rows)
                result.elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
                result.source = source
                results.append(result)
            except SQLError as e:
                results.append(Result("error", source, message=str(e)))
        return results

    def run(self, stmt, use_index=True, max_rows=None):
        lock = self.locks.get(stmt.table)
        if lock is None:                                # 없는 테이블 → 락 없이 바로 오류
            self.get_table(stmt.table)
        if isinstance(stmt, Insert):
            with lock.write():                          # [Phase 3] INSERT는 혼자만
                return self.run_insert(stmt)
        with lock.read():                               # [Phase 3] SELECT는 여럿이 동시에
            result = self.run_select(stmt, use_index, max_rows)
            if not isinstance(result.rows, list):
                result.rows = list(result.rows)
            return result

    def get_table(self, name):
        if name not in self.tables:
            raise SQLError(f"'{name}' 테이블이 없어요 (있는 테이블: {', '.join(self.tables)})")
        return self.tables[name]

    # ── INSERT ──
    def run_insert(self, stmt):
        table = self.get_table(stmt.table)
        all_cols = [c for c, _ in table.columns]

        # 컬럼 목록 정하기
        if stmt.columns is None:
            target = all_cols                          # 생략하면 id 뺀 전체 컬럼 순서대로
        else:
            target = stmt.columns
            for col in target:
                if col == "id":
                    raise SQLError("id는 자동으로 부여돼서 직접 넣을 수 없어요")
                if col not in all_cols:
                    raise SQLError(f"'{stmt.table}' 테이블에 '{col}' 컬럼이 없어요")
            if len(set(target)) != len(target):
                raise SQLError("같은 컬럼을 두 번 적었어요")

        # 모든 행을 먼저 검사하고, 다 통과해야 저장 (일부만 저장되는 일 방지)
        types = dict(table.columns)
        records = []
        for n, row in enumerate(stmt.rows, 1):
            if len(row) != len(target):
                raise SQLError(f"{n}번째 행: 컬럼은 {len(target)}개인데 값은 {len(row)}개예요")
            given = dict(zip(target, row))
            values = []
            for col in all_cols:
                v = given.get(col)                     # 안 적은 컬럼은 NULL
                check_type(col, types[col], v)
                values.append(v)
            records.append(values)

        ids = table.insert(records)
        if len(ids) == 1:
            msg = f"1개 행 추가 (id = {ids[0]})"
        else:
            msg = f"{len(ids)}개 행 추가 (id = {ids[0]} ~ {ids[-1]})"
        return Result("insert", message=msg)

    # ── SELECT ──
    def run_select(self, stmt, use_index=True, max_rows=None):
        table = self.get_table(stmt.table)
        all_cols = ["id"] + [c for c, _ in table.columns]
        types = {"id": "INT", **dict(table.columns)}

        # 보여줄 컬럼
        if stmt.columns == "*":
            out_cols = all_cols
        else:
            for col in stmt.columns:
                if col not in types:
                    raise SQLError(f"'{stmt.table}' 테이블에 '{col}' 컬럼이 없어요")
            out_cols = stmt.columns

        # WHERE 조건 검사 (컬럼이 있는지, 타입이 맞는지)
        for cond in stmt.where:
            if cond.column not in types:
                raise SQLError(f"'{stmt.table}' 테이블에 '{cond.column}' 컬럼이 없어요")
            if cond.value is not None:
                check_type(cond.column, types[cond.column], cond.value)

        index = {c: i for i, c in enumerate(all_cols)}
        rows = []
        if stmt.limit == 0:
            return Result("select", columns=out_cols, rows=rows, plan="LIMIT 0 (읽지 않음)")

        # [Phase 2] 어떤 행을 읽을지 정하기
        id_range = id_range_from(stmt.where) if use_index else None
        if id_range is None:
            records = table.scan()                     # 파일을 처음부터 끝까지 (선형 탐색)
            plan = "선형 탐색 (파일 전체를 처음부터 읽음)"
        else:
            records = self.records_by_index(table, id_range)
            plan = describe_range(id_range)

        # 읽은 행 중 WHERE 조건에 모두 맞는 행만 고름
        examined = matched = 0
        for record in records:
            examined += 1
            if all(compare(record[index[c.column]], c.op, c.value) for c in stmt.where):
                matched += 1
                if max_rows is None or len(rows) < max_rows:
                    rows.append([record[index[c]] for c in out_cols])
                if stmt.limit is not None and matched >= stmt.limit:
                    break
        return Result("select", columns=out_cols, rows=rows, plan=plan, examined=examined,
                      total_rows=matched)

    # [Phase 2] B+ 트리에서 id 범위의 위치(오프셋)를 찾아, 그 줄들만 파일에서 읽음
    def records_by_index(self, table, id_range):
        lo, hi, lo_inc, hi_inc = id_range
        if id_range is EMPTY or len(table.index) == 0:
            return iter(())
        if lo is not None and lo == hi:                # id = 값 → 한 개만
            offset = table.index.search(lo)
            return table.read_at([] if offset is None else [offset])
        offsets = (off for _, off in table.index.range(lo, hi, lo_inc, hi_inc))
        return table.read_at(offsets)


EMPTY = (1, 0, True, True)      # 결과가 없을 수밖에 없는 범위 (예: id > 5 AND id < 3)


def id_range_from(where):
    """WHERE 조건 중 id 비교(=, <, <=, >, >=)를 모아서 (lo, hi, lo_포함, hi_포함) 범위로 합침
    id 조건이 없으면 None → 선형 탐색
      id = 5            → (5, 5, True, True)
      id > 3 AND id <= 9 → (3, 9, False, True)"""
    conds = [c for c in where if c.column == "id" and c.op in ("=", "<", "<=", ">", ">=")]
    if not conds:
        return None
    lo = hi = None
    lo_inc = hi_inc = True
    for c in conds:
        v = c.value
        if v is None:                                  # id = NULL 은 항상 거짓
            return EMPTY
        if c.op in ("=", ">", ">="):
            inc = c.op != ">"
            if lo is None or v > lo or (v == lo and not inc):
                lo, lo_inc = v, inc
        if c.op in ("=", "<", "<="):
            inc = c.op != "<"
            if hi is None or v < hi or (v == hi and not inc):
                hi, hi_inc = v, inc
    if lo is not None and hi is not None and (lo > hi or (lo == hi and not (lo_inc and hi_inc))):
        return EMPTY
    return (lo, hi, lo_inc, hi_inc)


def describe_range(id_range):
    if id_range is EMPTY:
        return "B+ 트리 인덱스 (조건상 결과 없음, 읽지 않음)"
    lo, hi, lo_inc, hi_inc = id_range
    if lo is not None and lo == hi:
        return f"B+ 트리 인덱스 (id = {lo} 바로 찾기)"
    left = "처음" if lo is None else f"{lo}{'' if lo_inc else ' 초과'}"
    right = "끝" if hi is None else f"{hi}{'' if hi_inc else ' 미만'}"
    return f"B+ 트리 인덱스 범위 (id {left} ~ {right}, 잎을 따라 옆으로 읽음)"


def check_type(col, typ, value):
    if value is None:
        return
    if typ == "INT" and not isinstance(value, int):
        raise SQLError(f"'{col}'은(는) INT 컬럼이라 숫자가 와야 해요 (받은 값: '{value}')")
    if typ == "TEXT" and not isinstance(value, str):
        raise SQLError(f"'{col}'은(는) TEXT 컬럼이라 '문자열'이 와야 해요 (받은 값: {value})")


def compare(a, op, b):
    # SQL 규칙: NULL과의 비교는 항상 거짓 (= NULL 도 거짓)
    if a is None or b is None:
        return False
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b
