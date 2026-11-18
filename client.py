# client.py
# [Phase 3] 외부 클라이언트: API 서버에 SQL을 보내고 결과를 받아서 출력
# (DB 파일을 직접 건드리지 않고, 오직 HTTP 요청만으로 미니 DBMS를 사용)
#
# 실행 (api_server.py가 켜져 있어야 함)
#   python client.py -e "SELECT * FROM users WHERE id = 1"
#   python client.py examples/sample.sql
#   python client.py                       대화형 모드
#   --url http://localhost:9000            서버 주소

import argparse
import json
import sys
import urllib.error
import urllib.request

from main import format_table


def send(url, sql, use_index=True):
    req = urllib.request.Request(url.rstrip("/") + "/query", method="POST",
                                 data=json.dumps({"sql": sql, "use_index": use_index}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def show(status, data):
    if status != 200:
        print(f"서버 오류 {status}: {data.get('error')}")
        return False
    ok = True
    print(f"[처리한 워커: {data['worker']} · 큐 대기 {data['queue_wait_ms']}ms]\n")
    for n, r in enumerate(data["results"], 1):
        if r["kind"] == "select":
            print(format_table(r["columns"], r["rows"]))
            more = f" (앞 {len(r['rows'])}개만 받음)" if r["total_rows"] > len(r["rows"]) else ""
            print(f"({r['total_rows']:,}개 행{more})")
            print(f"  실행: {r['plan']} · 읽은 행 {r['examined']:,}개 · {r['elapsed_ms']:,.3f}ms\n")
        elif r["kind"] == "insert":
            print(r["message"] + "\n")
        else:
            ok = False
            print(f"오류 [{n}번째 문장] {r['source']}\n  → {r['message']}\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="미니 DBMS API 클라이언트")
    ap.add_argument("file", nargs="?")
    ap.add_argument("-e", "--execute")
    ap.add_argument("--url", default="http://localhost:9000")
    ap.add_argument("--no-index", action="store_true")
    args = ap.parse_args()

    def run(sql):
        try:
            return show(*send(args.url, sql, not args.no_index))
        except urllib.error.URLError:
            print(f"서버에 연결할 수 없어요: {args.url} (api_server.py가 켜져 있나요?)")
            return False

    if args.execute:
        return 0 if run(args.execute) else 1
    if args.file:
        try:
            with open(args.file, encoding="utf-8-sig") as f:
                return 0 if run(f.read()) else 1
        except FileNotFoundError:
            print(f"파일이 없어요: {args.file}")
            return 1
    print(f"미니 DBMS 클라이언트 → {args.url} (문장은 ; 로 끝, 종료: exit)")
    buffer = ""
    while True:
        try:
            line = input("client> " if not buffer else "     -> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not buffer and line.strip().lower() in ("exit", "quit"):
            return 0
        buffer += line + "\n"
        if buffer.strip().endswith(";"):
            run(buffer)
            buffer = ""


if __name__ == "__main__":
    sys.exit(main())
