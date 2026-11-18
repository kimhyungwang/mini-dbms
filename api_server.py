# api_server.py
# [Phase 3] 미니 DBMS API 서버 (스레드 풀로 요청을 병렬 처리)
#
# 실행: python api_server.py                         → http://localhost:9000
#       python api_server.py --workers 8 --db bench_db
#
# 구조
#   클라이언트 ──HTTP──▶ [접수 스레드] ──▶ [스레드 풀 작업 큐] ──▶ 워커 N개
#                         연결만 받음        가득 차면 503             │
#                                                                     ▼
#                                     Database.execute(SQL)  ← Phase 1 + 2 엔진 그대로
#                                     (테이블별 읽기/쓰기 락으로 동시성 처리)
#
# API (모두 JSON)
#   POST /query   {"sql": "SELECT ...", "use_index": true}
#                 → {"results": [...], "worker": "worker-3", "queue_wait_ms": 0.1}
#   GET  /tables  테이블 목록 (컬럼, 행 수, B+ 트리 정보)
#   GET  /stats   스레드 풀 상태 (워커별 처리 중 SQL, 대기 수, 완료 수, 거절 수)
#   GET  /health  서버가 살아 있는지
#   GET  /        화면 (web/index.html)
#
# 오류 응답: 400 잘못된 요청 · 404 없는 주소 · 413 요청이 너무 큼 · 503 서버가 바쁨(큐 가득)

import argparse
import json
import os
import socket
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from executor import Database
from sql_parser import SQLError
from thread_pool import PoolFull, ThreadPool

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 1_000_000          # 요청 본문 최대 크기 (1MB)
# 파이썬은 GIL 때문에 한 순간에 스레드 하나만 파이썬 코드를 실행함.
# 기본값(5ms)이면 선형 탐색처럼 오래 걸리는 요청이 GIL을 5ms씩 붙잡고 있어서,
# 소켓을 읽고 쓰느라 GIL을 자주 내려놓는 빠른 요청(id 검색)이 매번 오래 기다림.
# 0.5ms로 줄이면 빠른 요청이 훨씬 자주 차례를 받음 (load_test.py 테스트 2 참고)
GIL_SWITCH_MS = 0.5
MAX_ROWS = 1000               # 한 결과에 돌려줄 최대 행 수
FILE_PREVIEW_LINES = 200


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniDBMS/1.0"

    # ── 응답 도우미 ──
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    @property
    def db(self):
        return self.server.db

    @property
    def pool(self):
        return self.server.pool

    # ── GET ──
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(BASE_DIR, "web", "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/health":
            self.send_json(200, {"status": "ok"})
        elif url.path == "/stats":
            self.send_json(200, self.pool.stats())
        elif url.path == "/tables":
            self.send_json(200, {"tables": table_list(self.db)})
        elif url.path == "/file":
            name = parse_qs(url.query).get("table", [""])[0]
            if name not in self.db.tables:
                return self.send_json(404, {"error": f"'{name}' 테이블이 없어요"})
            self.send_json(200, file_preview(self.db, name))
        else:
            self.send_json(404, {"error": "없는 주소예요"})

    # ── POST /query ──
    def do_POST(self):
        if urlparse(self.path).path != "/query":
            return self.send_json(404, {"error": "없는 주소예요"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send_json(400, {"error": "Content-Length가 잘못됐어요"})
        if length > MAX_BODY:
            return self.send_json(413, {"error": f"요청이 너무 커요 (최대 {MAX_BODY:,}바이트)"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return self.send_json(400, {"error": "JSON 형식이 아니에요. 예: {\"sql\": \"SELECT * FROM users\"}"})
        sql = body.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return self.send_json(400, {"error": "sql이 필요해요"})
        use_index = body.get("use_index", True) is not False

        self.pool.set_info(sql.strip()[:80])            # 상태 화면에 "이 워커가 실행 중인 SQL" 표시
        try:
            results = [asdict(r) for r in self.db.execute(sql, use_index=use_index, max_rows=MAX_ROWS)]
        except SQLError as e:                          # 따옴표가 안 닫히는 등 전체를 못 읽는 경우
            results = [{"kind": "error", "source": sql.strip(), "columns": [], "rows": [],
                        "message": str(e), "plan": "", "examined": 0, "elapsed_ms": 0, "total_rows": 0}]
        self.send_json(200, {"results": results,
                             "worker": threading.current_thread().name,
                             "queue_wait_ms": self.pool.current_wait_ms(),
                             "max_rows": MAX_ROWS})


class PoolHTTPServer(HTTPServer):
    """연결이 들어오면 새 스레드를 만들지 않고 스레드 풀 큐에 넣음"""

    def __init__(self, addr, db, pool, request_timeout=10):
        super().__init__(addr, Handler)
        self.db = db
        self.pool = pool
        self.request_timeout = request_timeout

    # 접수 스레드: 연결을 받으면 여기로 옴 → 처리는 워커에게 맡기고 바로 다음 연결을 받으러 감
    def process_request(self, request, client_address):
        try:
            self.pool.submit(self._handle, request, client_address,
                             info=f"{client_address[0]}:{client_address[1]} 요청 읽는 중")
        except (PoolFull, RuntimeError):
            # 큐가 가득 참 → 줄 세우지 않고 바로 503 (요청이 끝없이 쌓여 메모리가 터지는 것 방지)
            body = json.dumps({"error": "서버가 바빠요. 잠시 후 다시 시도하세요"}, ensure_ascii=False).encode()
            try:
                # 클라이언트가 이미 보낸 요청을 아주 잠깐만 읽어서 버림
                # (안 읽고 닫으면 연결이 강제로 끊겨서 클라이언트가 503 응답조차 못 받음)
                request.settimeout(0.05)
                try:
                    while len(request.recv(65536)) == 65536:
                        pass
                except OSError:
                    pass
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                                + f"Content-Length: {len(body)}\r\nRetry-After: 1\r\n\r\n".encode() + body)
            except OSError:
                pass
            self.shutdown_request(request)

    # 워커 스레드: 실제로 요청을 읽고 처리하고 응답
    def _handle(self, request, client_address):
        request.settimeout(self.request_timeout)       # 요청을 아주 천천히 보내는 클라이언트가 워커를 붙잡지 못하게
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout, TimeoutError)):
            return                                      # 클라이언트가 끊었거나 너무 느림 → 조용히 정리
        super().handle_error(request, client_address)


# ── 테이블 정보 (화면용) ──

def table_list(db):
    out = []
    for name, table in db.tables.items():
        with db.locks[name].read():
            out.append({
                "name": name,
                "columns": [{"name": "id", "type": "INT"}] + [{"name": c, "type": t} for c, t in table.columns],
                "rows": table.row_count,
                "next_id": table.next_id,
                "file": os.path.basename(table.path),
                "index": table.index.stats(),
            })
    return out


def file_preview(db, name):
    table = db.tables[name]
    lines = []
    with db.locks[name].read():
        if os.path.exists(table.path):
            with open(table.path, encoding="utf-8") as f:
                for line in f:
                    lines.append(line.rstrip("\n"))
                    if len(lines) >= FILE_PREVIEW_LINES:
                        break
        return {"file": os.path.basename(table.path), "lines": lines, "total": table.row_count,
                "meta": os.path.basename(table.meta_path), "next_id": table.next_id}


def make_server(db_dir="db", port=9000, workers=8, queue_size=100, request_timeout=10, host="localhost"):
    db = Database(db_dir)
    pool = ThreadPool(workers=workers, queue_size=queue_size)
    return PoolHTTPServer((host, port), db, pool, request_timeout)


def main():
    ap = argparse.ArgumentParser(description="미니 DBMS API 서버 (스레드 풀)")
    ap.add_argument("--db", default="db", help="데이터 폴더 (기본: db)")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--workers", type=int, default=8, help="스레드 풀 워커 수 (기본: 8)")
    ap.add_argument("--queue", type=int, default=100, help="작업 큐 최대 크기 (기본: 100)")
    ap.add_argument("--switch-ms", type=float, default=GIL_SWITCH_MS,
                    help=f"GIL 전환 간격 ms (기본: {GIL_SWITCH_MS}, 파이썬 기본값은 5)")
    args = ap.parse_args()
    sys.setswitchinterval(args.switch_ms / 1000)

    start = time.perf_counter()
    try:
        server = make_server(args.db, args.port, args.workers, args.queue)
    except SQLError as e:
        print(f"오류: {e}")
        return
    rows = sum(t.row_count for t in server.db.tables.values())
    print(f"DB 열기 + B+ 트리 만들기: {time.perf_counter() - start:.2f}초 (전체 {rows:,}행)")
    print(f"미니 DBMS API 서버 → http://localhost:{args.port}  "
          f"(워커 {args.workers}개, 큐 {args.queue}개, 종료: Ctrl+C)")

    # 접수 스레드에서 서버를 돌리고, 메인 스레드는 Ctrl+C만 기다림 (윈도우에서도 바로 꺼지게)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n종료 중… (처리 중인 요청은 끝까지 처리)")
        server.shutdown()                  # 새 연결 그만 받기
        server.pool.shutdown(wait=True)    # 큐에 남은 요청까지 처리하고 워커 종료
        server.server_close()
        print("종료")


if __name__ == "__main__":
    main()
