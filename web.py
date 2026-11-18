# web.py
# 브라우저에서 미니 DBMS를 써 보는 화면 (터미널 대신 보기 편하게)
#
# 실행: python web.py  →  브라우저에서 http://localhost:8080
#
#   GET  /                    화면 (web/index.html)
#   POST /query  {"sql": ..., "use_index": true}  SQL 실행 → 결과 목록 (use_index=false면 항상 선형 탐색)
#   GET  /tables              테이블 목록 (컬럼, 타입, 행 개수, 다음 id, B+ 트리 정보)
#   GET  /file?table=users    테이블 파일 내용 그대로 (저장 형식 확인용)
#
# CLI(main.py)와 같은 Database.execute()를 부름 → 입구만 다르고 엔진은 같음

import argparse
import json
import os
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from executor import Database
from sql_parser import SQLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILE_PREVIEW_LINES = 200        # 파일 보기에서 보여줄 최대 줄 수
MAX_ROWS = 1000                 # 화면으로 보낼 최대 행 수 (100만 행을 그리면 브라우저가 멈춤)

db = None                       # 실행할 때 만들어짐
db_lock = threading.Lock()      # 요청은 여러 스레드가 받지만, DB는 한 번에 한 요청만 만지게 함


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif url.path == "/tables":
            with db_lock:
                tables = table_list()
            self.send_json(200, {"tables": tables})
        elif url.path == "/file":
            name = parse_qs(url.query).get("table", [""])[0]
            if name not in db.tables:
                return self.send_json(404, {"error": f"'{name}' 테이블이 없어요"})
            with db_lock:
                preview = file_preview(db.tables[name])
            self.send_json(200, preview)
        else:
            self.send_json(404, {"error": "없는 주소예요"})

    def do_POST(self):
        if urlparse(self.path).path != "/query":
            return self.send_json(404, {"error": "없는 주소예요"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            sql = body.get("sql", "")
            use_index = body.get("use_index", True) is not False
        except (ValueError, json.JSONDecodeError, AttributeError):
            return self.send_json(400, {"error": "JSON 형식이 아니에요"})
        if not isinstance(sql, str) or not sql.strip():
            return self.send_json(400, {"error": "SQL을 입력하세요"})
        try:
            with db_lock:
                results = db.execute(sql, use_index=use_index, max_rows=MAX_ROWS)
        except SQLError as e:                       # 따옴표가 안 닫히는 등 전체를 못 읽는 경우
            return self.send_json(200, {"results": [{"kind": "error", "source": sql.strip(),
                                                     "columns": [], "rows": [], "message": str(e)}]})
        self.send_json(200, {"results": [asdict(r) for r in results], "max_rows": MAX_ROWS})

    def log_message(self, format, *args):
        pass                                         # 터미널 로그 끔


def table_list():
    out = []
    for name, table in db.tables.items():
        out.append({
            "name": name,
            "columns": [{"name": "id", "type": "INT"}] + [{"name": c, "type": t} for c, t in table.columns],
            "rows": table.row_count,                     # 인덱스 키 개수 (파일을 다 세지 않아도 됨)
            "next_id": table.next_id,
            "file": os.path.basename(table.path),
            "index": table.index.stats(),
        })
    return out


def file_preview(table):
    lines = []
    if os.path.exists(table.path):
        with open(table.path, encoding="utf-8") as f:
            for line in f:
                lines.append(line.rstrip("\n"))
                if len(lines) >= FILE_PREVIEW_LINES:
                    break
    return {"file": os.path.basename(table.path), "lines": lines, "total": table.row_count,
            "meta": os.path.basename(table.meta_path), "next_id": table.next_id}


class Server(ThreadingHTTPServer):
    # 요청마다 스레드를 따로 띄움
    # → 브라우저가 미리 열어 둔 빈 연결 때문에 다른 요청이 막히는 일이 없음
    daemon_threads = True

    # 브라우저가 응답을 받는 도중에 연결을 끊은 경우(새로고침, 탭 닫기 등)는
    # 서버 문제가 아니라서 긴 오류 메시지를 찍지 않음
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    global db
    ap = argparse.ArgumentParser(description="미니 DBMS 웹 화면")
    ap.add_argument("--db", default="db", help="데이터 폴더 (기본: db)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    try:
        db = Database(args.db)
    except SQLError as e:
        print(f"오류: {e}")
        return
    server = Server(("localhost", args.port), Handler)
    print(f"미니 DBMS 화면 → http://localhost:{args.port}  (종료: Ctrl+C)")
    # 서버는 별도 스레드에서 돌리고, 메인 스레드는 Ctrl+C만 기다림
    # (윈도우에서는 serve_forever()가 연결을 기다리는 동안 Ctrl+C를 못 받는 경우가 있음)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.shutdown()
        print("\n종료")


if __name__ == "__main__":
    main()
