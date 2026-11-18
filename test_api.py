# test_api.py — [Phase 3] 기능 테스트: API 서버를 실제로 띄우고 HTTP로 요청
# 실행: python -m unittest -v

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from api_server import make_server


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "schema.json"), "w", encoding="utf-8") as f:
            json.dump({"users": {"name": "TEXT", "age": "INT"}}, f)
        self.server = make_server(self.dir, port=0, workers=4, queue_size=50, request_timeout=1)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://localhost:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.pool.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir)

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def query(self, sql, **extra):
        return self.request("POST", "/query", {"sql": sql, **extra})

    # ── 정상 동작 ──
    def test_insert_and_select(self):
        status, data = self.query("INSERT INTO users VALUES ('철수', 20); SELECT * FROM users WHERE id = 1;")
        self.assertEqual(status, 200)
        self.assertEqual(data["results"][0]["message"], "1개 행 추가 (id = 1)")
        self.assertEqual(data["results"][1]["rows"], [[1, "철수", 20]])
        self.assertIn("B+ 트리", data["results"][1]["plan"])
        self.assertTrue(data["worker"].startswith("worker-"))

    def test_use_index_false(self):
        self.query("INSERT INTO users VALUES ('a', 1)")
        self.assertIn("선형 탐색", self.query("SELECT * FROM users WHERE id = 1", use_index=False)[1]["results"][0]["plan"])

    def test_sql_errors_are_200_with_error_result(self):
        status, data = self.query("SELECT * FROM nope")
        self.assertEqual(status, 200)
        self.assertEqual(data["results"][0]["kind"], "error")

    def test_unclosed_quote(self):
        status, data = self.query("INSERT INTO users VALUES ('abc")
        self.assertEqual(data["results"][0]["kind"], "error")

    def test_rows_capped(self):
        self.query("INSERT INTO users VALUES " + ", ".join(f"('u{i}', {i})" for i in range(1500)))
        r = self.query("SELECT * FROM users")[1]["results"][0]
        self.assertEqual((len(r["rows"]), r["total_rows"]), (1000, 1500))

    def test_other_endpoints(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", "/stats")[1]["max_workers"], 4)
        self.assertEqual(self.request("GET", "/tables")[1]["tables"][0]["name"], "users")

    # ── 잘못된 요청 ──
    def test_bad_requests(self):
        self.assertEqual(self.request("POST", "/query", raw=b"not json")[0], 400)
        self.assertEqual(self.request("POST", "/query", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request("POST", "/query", {"x": 1})[0], 400)
        self.assertEqual(self.request("POST", "/query", {"sql": "   "})[0], 400)
        self.assertEqual(self.request("POST", "/query", {"sql": 123})[0], 400)
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {"sql": "x"})[0], 404)

    def test_too_large(self):
        with socket.create_connection(self.server.server_address) as s:
            s.sendall(b"POST /query HTTP/1.0\r\nContent-Length: 5000000\r\n\r\n")
            self.assertIn(b"413", s.recv(100))

    # ── 동시성 ──
    def test_concurrent_inserts(self):
        statuses = []

        def client(n):
            for i in range(20):
                statuses.append(self.query(f"INSERT INTO users VALUES ('c{n}-{i}', {i})")[0])

        threads = [threading.Thread(target=client, args=(n,)) for n in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        r = self.query("SELECT id FROM users")[1]["results"][0]
        self.assertEqual(statuses, [200] * 200)
        self.assertEqual(sorted(row[0] for row in r["rows"]), list(range(1, 201)))

    def test_slow_client_does_not_block_others(self):
        # 요청을 끝까지 안 보내는 클라이언트가 워커 하나를 잡고 있어도 다른 요청은 처리됨
        slow = socket.create_connection(self.server.server_address)
        slow.sendall(b"POST /query HTTP/1.0\r\n")       # 일부만 보내고 멈춤
        start = time.perf_counter()
        self.assertEqual(self.query("SELECT * FROM users")[0], 200)
        self.assertLess(time.perf_counter() - start, 0.5)
        slow.close()

    def test_full_queue_returns_503(self):
        self.tearDown()
        self.setUp_small()                               # 워커 1개, 큐 1칸짜리 서버

        def wait_until(busy, queued):
            for _ in range(100):
                st = self.server.pool.stats()
                if st["busy"] == busy and st["queued"] == queued:
                    return
                time.sleep(0.01)
            self.fail(f"상태가 안 바뀜: {st['busy']}, {st['queued']}")

        # 요청을 끝까지 안 보내는 느린 연결로 워커 1개 → 큐 1칸 순서대로 채움
        slow1 = socket.create_connection(self.server.server_address)
        slow1.sendall(b"POST /query HTTP/1.0\r\n")
        wait_until(busy=1, queued=0)
        slow2 = socket.create_connection(self.server.server_address)
        slow2.sendall(b"POST /query HTTP/1.0\r\n")
        wait_until(busy=1, queued=1)
        self.assertEqual(self.request("GET", "/health")[0], 503)   # 더 들어갈 자리가 없음 → 바로 거절
        self.assertEqual(self.server.pool.stats()["rejected"], 1)
        slow1.close()
        slow2.close()

    def setUp_small(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "schema.json"), "w", encoding="utf-8") as f:
            json.dump({"users": {"name": "TEXT", "age": "INT"}}, f)
        self.server = make_server(self.dir, port=0, workers=1, queue_size=1, request_timeout=2)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://localhost:{self.server.server_address[1]}"


if __name__ == "__main__":
    unittest.main(verbosity=2)
