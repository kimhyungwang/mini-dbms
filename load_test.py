# load_test.py
# [Phase 3] 동시 요청 부하 테스트 (서버를 직접 띄워서 확인, 따로 켤 필요 없음)
#
# 실행: python load_test.py
#
# 테스트 1. 정합성: 클라이언트 여러 명이 동시에 INSERT → 행 수와 id가 정확한가
# 테스트 2. 워커 수 비교: 느린 요청(선형 탐색)이 계속 들어오는 중에 빠른 요청(id 검색)이 얼마나 기다리나
#          워커 1개 → 빠른 요청도 느린 요청 뒤에 줄 서서 기다림
#          워커 8개 → 다른 워커가 빠른 요청을 바로 처리
# 테스트 3. 과부하: 큐보다 많은 요청이 몰리면 503으로 바로 거절하는가

import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from executor import Database

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = {"users": {"name": "TEXT", "age": "INT", "email": "TEXT"}}


def post(url, sql, timeout=60):
    req = urllib.request.Request(url + "/query", method="POST", data=json.dumps({"sql": sql}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError):
        return "연결 오류", None


def start(db_dir, workers, queue=100, switch_ms=None):
    """서버를 별도 프로세스로 실행 (실제처럼 클라이언트와 서버가 다른 프로그램)"""
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "api_server.py"), "--db", db_dir,
                             "--port", str(port), "--workers", str(workers), "--queue", str(queue)]
                            + ([] if switch_ms is None else ["--switch-ms", str(switch_ms)]),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://localhost:{port}"
    for _ in range(100):                                   # 켜질 때까지 기다림
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            return proc, url
        except OSError:
            time.sleep(0.1)
    raise SystemExit("서버가 켜지지 않았어요")


def stop(proc):
    proc.terminate()
    proc.wait()


def stats(url):
    with urllib.request.urlopen(url + "/stats") as res:
        return json.loads(res.read())


def make_db(rows):
    d = tempfile.mkdtemp()
    with open(f"{d}/schema.json", "w", encoding="utf-8") as f:
        json.dump(SCHEMA, f)
    db = Database(d)
    for start_id in range(0, rows, 1000):
        values = ", ".join(f"('user{k}', {k % 70 + 10}, 'user{k}@mail.com')"
                           for k in range(start_id + 1, min(rows, start_id + 1000) + 1))
        db.execute(f"INSERT INTO users VALUES {values}")
    return d


def run_parallel(fn, n):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_consistency():
    print("■ 테스트 1. 동시 INSERT 정합성")
    d = make_db(0)
    server, url = start(d, workers=8)
    clients, each = 20, 50
    statuses = []

    def client(i):
        for k in range(each):
            statuses.append(post(url, f"INSERT INTO users VALUES ('c{i}-{k}', {k}, 'x')")[0])

    t = time.perf_counter()
    run_parallel(client, clients)
    elapsed = time.perf_counter() - t
    ids = [r[0] for r in post(url, "SELECT id FROM users")[1]["results"][0]["rows"]]
    total = post(url, "SELECT id FROM users")[1]["results"][0]["total_rows"]
    stop(server)
    shutil.rmtree(d)
    expected = clients * each
    print(f"  클라이언트 {clients}명 × INSERT {each}번 = {expected}개 ({elapsed:.2f}초)")
    print(f"  성공 응답 {statuses.count(200)}/{expected} · 저장된 행 {total} · "
          f"id 중복 {len(ids) - len(set(ids))}개 · id 범위 {min(ids)}~{max(ids)}")
    ok = statuses.count(200) == expected and total == expected and len(set(ids)) == len(ids)
    print(f"  → {'✓ 하나도 빠지거나 겹치지 않음' if ok else '✗ 문제 있음'}\n")
    return ok


def test_workers(rows=100_000, seconds=4):
    print(f"■ 테스트 2. 워커 수에 따른 빠른 요청 대기 시간 ({rows:,}행)")
    print(f"  느린 요청: 선형 탐색(name = ?)을 2명이 계속 보냄 · 빠른 요청: id 검색을 4명이 {seconds}초 동안 보냄")
    d = make_db(rows)
    summary = {}
    cases = [(1, None, "워커 1개"), (8, 5, "워커 8개 (GIL 전환 5ms, 파이썬 기본)"), (8, None, "워커 8개 (GIL 전환 0.5ms)")]
    for workers, switch_ms, label in cases:
        server, url = start(d, workers=workers, switch_ms=switch_ms)
        stop_at = time.perf_counter() + seconds
        fast, slow = [], []

        def slow_client(i):
            while time.perf_counter() < stop_at:
                t = time.perf_counter()
                post(url, "SELECT * FROM users WHERE name = 'nobody'")
                slow.append((time.perf_counter() - t) * 1000)

        def fast_client(i):
            k = 1
            while time.perf_counter() < stop_at:
                t = time.perf_counter()
                post(url, f"SELECT * FROM users WHERE id = {k * 7919 % rows + 1}")
                fast.append((time.perf_counter() - t) * 1000)
                k += 1

        threads = [threading.Thread(target=slow_client, args=(i,)) for i in range(2)] + \
                  [threading.Thread(target=fast_client, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop(server)
        fast.sort()
        summary[label] = (statistics.median(fast), fast[int(len(fast) * 0.95)], len(fast), len(slow))
        print(f"  {label}: id 검색 {len(fast):>4}번 · 중간값 {summary[label][0]:6.1f}ms · "
              f"느린 쪽 5% {summary[label][1]:6.1f}ms · (선형 탐색 {len(slow)}번)")
    shutil.rmtree(d)
    one, best = summary["워커 1개"], summary["워커 8개 (GIL 전환 0.5ms)"]
    print(f"  → 워커 8개 + GIL 0.5ms: id 검색 처리량 {best[2] / max(1, one[2]):.1f}배, "
          f"중간 응답 시간 {one[0] / best[0]:.0f}배 짧음\n")


def test_overload():
    print("■ 테스트 3. 과부하 시 거절 (워커 1개, 큐 2개에 요청 20개를 한꺼번에)")
    d = make_db(30_000)
    server, url = start(d, workers=1, queue=2)
    codes = []
    run_parallel(lambda i: codes.append(post(url, "SELECT * FROM users WHERE name = 'x'")[0]), 20)
    rejected = stats(url)["rejected"]
    stop(server)
    shutil.rmtree(d)
    print(f"  200 성공 {codes.count(200)}개 · 503 거절 {codes.count(503)}개 (서버 기록: 거절 {rejected}개)")
    print(f"  → {'✓ 감당 못 하는 요청은 줄 세우지 않고 바로 503' if codes.count(503) > 0 else '✗'}\n")


if __name__ == "__main__":
    test_consistency()
    test_workers()
    test_overload()
