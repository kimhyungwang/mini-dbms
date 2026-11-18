# bench.py
# [Phase 2] 대량 데이터 넣기 + 인덱스 사용 / 미사용 속도 비교
#
# 실행: python bench.py                 100만 건 (처음 한 번은 넣는 데 시간이 걸림, 다음부턴 바로 비교)
#       python bench.py --rows 100000   10만 건으로 빠르게
#
# - 데이터는 Phase 1의 SQL 처리기(Database.execute)로 넣음
#     INSERT INTO users VALUES ('user1', 23, 'user1@mail.com'), ('user2', ...), ... ← 1000행씩 묶음
# - 비교용 데이터는 db/ 가 아니라 bench_db/ 폴더에 따로 만듦 (화면에서 쓰는 데이터와 안 섞이게)

import argparse
import json
import os
import random
import statistics
import time

from executor import Database
from main import pad

SCHEMA = {"users": {"name": "TEXT", "age": "INT", "email": "TEXT"}}


def fill(db, target, batch):
    """users 테이블이 target개가 될 때까지 SQL INSERT로 채움"""
    table = db.tables["users"]
    have = table.row_count
    if have >= target:
        print(f"이미 {have:,}개 있어요. 넣기는 건너뜀\n")
        return
    print(f"{have:,}개 → {target:,}개까지 SQL INSERT로 넣는 중 (한 문장에 {batch}행씩)")
    rng = random.Random(42)
    start = time.perf_counter()
    i = have
    while i < target:
        n = min(batch, target - i)
        values = ", ".join(
            f"('user{k}', {rng.randint(10, 80)}, 'user{k}@mail.com')" for k in range(i + 1, i + n + 1))
        result = db.execute(f"INSERT INTO users VALUES {values};")[0]
        if result.kind == "error":
            raise SystemExit(f"INSERT 실패: {result.message}")
        i += n
        if i % 100_000 == 0 or i == target:
            print(f"  {i:>9,}개  ({time.perf_counter() - start:5.1f}초)")
    print(f"넣기 완료: {time.perf_counter() - start:.1f}초\n")


def measure(db, sql, use_index, repeat):
    """같은 SQL을 repeat번 실행해서 평균 시간(ms)과 마지막 결과를 돌려줌"""
    times, result = [], None
    for _ in range(repeat):
        result = db.execute(sql, use_index=use_index)[0]
        times.append(result.elapsed_ms)
    return statistics.mean(times), result


def main():
    ap = argparse.ArgumentParser(description="미니 DBMS 인덱스 성능 비교")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=1000, help="INSERT 한 문장에 넣을 행 수")
    ap.add_argument("--db", default="bench_db")
    ap.add_argument("--linear-repeat", type=int, default=3, help="선형 탐색 반복 횟수 (느려서 적게)")
    ap.add_argument("--index-repeat", type=int, default=1000, help="인덱스 검색 반복 횟수")
    args = ap.parse_args()

    os.makedirs(args.db, exist_ok=True)
    with open(os.path.join(args.db, "schema.json"), "w", encoding="utf-8") as f:
        json.dump(SCHEMA, f)

    start = time.perf_counter()
    db = Database(args.db)
    table = db.tables["users"]
    print(f"테이블 열기 + B+ 트리 인덱스 만들기: {time.perf_counter() - start:.2f}초 "
          f"({table.row_count:,}개)\n")

    fill(db, args.rows, args.batch)
    stats = table.index.stats()
    print(f"B+ 트리: 키 {stats['keys']:,}개 · 높이 {stats['height']} · 잎 {stats['leaves']:,}개 "
          f"· 노드당 최대 {stats['max_keys']}개\n")

    n = table.row_count
    target = random.Random(7).randint(n // 2, n)              # 파일 뒤쪽에 있는 행 (선형 탐색에 불리한 쪽)
    cases = [
        ("id = ? (인덱스)", f"SELECT * FROM users WHERE id = {target}", True, args.index_repeat),
        ("id = ? (인덱스 끔)", f"SELECT * FROM users WHERE id = {target}", False, args.linear_repeat),
        ("name = ? (선형 탐색)", f"SELECT * FROM users WHERE name = 'user{target}'", True, args.linear_repeat),
        ("id 범위 100개 (인덱스)", f"SELECT * FROM users WHERE id >= {target - 99} AND id <= {target}", True, 100),
        ("id 범위 100개 (인덱스 끔)", f"SELECT * FROM users WHERE id >= {target - 99} AND id <= {target}", False, args.linear_repeat),
    ]

    print(f"찾는 행: id = {target:,} (전체 {n:,}개 중)\n")
    print(pad("조회", 30) + pad("평균 시간", 14) + pad("읽은 행", 12) + "결과")
    print("-" * 64)
    results = {}
    for name, sql, use_index, repeat in cases:
        ms, r = measure(db, sql, use_index, repeat)
        results[name] = (ms, r)
        print(pad(name, 30) + pad(f"{ms:,.3f}ms", 14) + pad(f"{r.examined:,}", 12) + str(len(r.rows)))

    fast = results["id = ? (인덱스)"]
    slow = results["id = ? (인덱스 끔)"]
    name_scan = results["name = ? (선형 탐색)"]
    assert fast[1].rows == slow[1].rows == name_scan[1].rows, "결과가 서로 달라요!"
    print("-" * 64)
    print(f"같은 행을 찾았는지: ✓ (id {target:,}, {fast[1].rows[0][1]})")
    print(f"id = ? 인덱스 사용 시 약 {slow[0] / fast[0]:,.0f}배 빠름 "
          f"({slow[0]:,.1f}ms → {fast[0]:.3f}ms)")


if __name__ == "__main__":
    main()
