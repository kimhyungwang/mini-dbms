# storage.py
# 테이블 데이터를 파일로 저장하고 읽는 부분
#
# 테이블 하나 = 파일 2개
#   users.jsonl      데이터. 한 줄 = 행 하나 = JSON 배열 [id, 컬럼1, 컬럼2, ...]
#                      [1, "철수", 20, "chulsoo@mail.com"]
#                      [2, "영희", null, null]
#   users.meta.json  다음에 줄 id  {"next_id": 3}
#
# JSON Lines 형식을 고른 이유
#   - 한 줄에 행 하나라서 INSERT = 파일 끝에 한 줄 추가 (앞부분은 안 건드림)
#   - 문자열 안의 쉼표, 따옴표, 줄바꿈, 한글, NULL을 따로 처리하지 않아도 JSON이 알아서 처리
#     (CSV는 빈 문자열 ''과 NULL을 구분하기 어려움)
#   - 메모장으로 열어도 사람이 읽을 수 있음
#
# [Phase 2] B+ 트리 인덱스
#   id → 그 행이 파일에서 시작하는 위치(바이트 오프셋)
#     1 → 0      (파일 맨 앞)
#     2 → 41     (41번째 바이트부터)
#   WHERE id = 2 이면 파일을 처음부터 읽지 않고, 41번째 바이트로 바로 이동(seek)해서 그 줄만 읽음
#   인덱스는 메모리에만 있어서, 프로그램이 켜질 때 파일을 한 번 훑어서 다시 만듦

import json
import os

from bplustree import BPlusTree


class Table:
    def __init__(self, data_dir, name, columns):
        self.name = name
        self.columns = columns                          # [("name", "TEXT"), ("age", "INT"), ...] (id 제외)
        self.path = os.path.join(data_dir, f"{name}.jsonl")
        self.meta_path = os.path.join(data_dir, f"{name}.meta.json")
        self.index = BPlusTree()
        self._build_index()                             # [Phase 2] 파일을 훑어서 id → 위치 인덱스 만들기
        self.next_id = self._load_next_id()

    # ── [Phase 2] 인덱스 ──

    def _build_index(self):
        for offset, line in self._lines():
            row_id = _read_id(line)
            if row_id is not None:
                self.index.insert(row_id, offset)

    def _lines(self):
        """파일을 바이트로 한 줄씩 읽으며 (그 줄이 시작하는 위치, 줄 내용)"""
        if not os.path.exists(self.path):
            return
        offset = 0
        with open(self.path, "rb") as f:
            for line in f:
                yield offset, line
                offset += len(line)

    # 다음에 줄 id: meta 파일 값과 (인덱스의 가장 큰 id + 1) 중 큰 쪽
    def _load_next_id(self):
        from_index = (self.index.max_key() or 0) + 1
        if os.path.exists(self.meta_path):
            with open(self.meta_path, encoding="utf-8") as f:
                return max(json.load(f)["next_id"], from_index)
        return from_index

    def _save_next_id(self):
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump({"next_id": self.next_id}, f)

    @property
    def row_count(self):
        return len(self.index)

    # ── 쓰기 ──

    # 행 여러 개를 파일 끝에 추가하고, 붙인 id 목록을 돌려줌
    # rows: [[값, 값, ...], ...] (id 없이, 컬럼 순서대로)
    def insert(self, rows):
        ids, lines = [], []
        for values in rows:
            ids.append(self.next_id)
            lines.append((json.dumps([self.next_id, *values], ensure_ascii=False) + "\n").encode("utf-8"))
            self.next_id += 1
        with open(self.path, "ab") as f:
            f.seek(0, os.SEEK_END)
            offset = f.tell()
            if offset > 0 and not _ends_with_newline(self.path):
                f.write(b"\n")                          # 반쯤 써진 줄 뒤에 바로 붙지 않게 줄을 바꿈
                offset += 1
            f.write(b"".join(lines))
        for row_id, line in zip(ids, lines):            # [Phase 2] 새 행의 위치를 인덱스에 등록
            self.index.insert(row_id, offset)
            offset += len(line)
        self._save_next_id()
        return ids

    # ── 읽기 ──

    # 파일을 처음부터 끝까지 한 줄씩 읽어서 행을 하나씩 돌려줌 (선형 탐색)
    # 꺼지는 순간 반쯤 써진 줄처럼 깨진 줄은 건너뜀
    def scan(self):
        for _, line in self._lines():
            record = _parse(line)
            if record is not None:
                yield record

    # [Phase 2] 위치(오프셋) 목록의 행만 읽기 (파일은 한 번만 열고, 위치로 바로 이동)
    def read_at(self, offsets):
        with open(self.path, "rb") as f:
            for offset in offsets:
                f.seek(offset)
                record = _parse(f.readline())
                if record is not None:
                    yield record


def _parse(line):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_id(line):
    """줄 전체를 JSON으로 풀지 않고 맨 앞 id만 빠르게 읽음: b'[123, "철수", ...]' → 123
    깨진 줄(id 뒤에 쉼표나 ]가 없음)이면 None"""
    end = line.find(b",")
    if end == -1:
        end = line.find(b"]")
    if not line.startswith(b"[") or end == -1:
        return None
    try:
        row_id = int(line[1:end])
    except ValueError:
        return None
    # 완성된 줄은 ] 로 끝남. 반쯤 써진 줄은 인덱스에 넣지 않음
    return row_id if line.rstrip().endswith(b"]") else None


def _ends_with_newline(path):
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"
