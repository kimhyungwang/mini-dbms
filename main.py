# main.py
# CLI (Command Line Interface): 터미널에서 SQL을 미니 DBMS에 전달하는 입구
#
# 사용법
#   python main.py examples/sample.sql        SQL 파일 실행
#   python main.py -e "SELECT * FROM users"   SQL 한 줄 바로 실행
#   python main.py                            대화형 모드 (한 문장씩 입력, ; 로 끝, exit 로 종료)
#   --db 폴더                                  데이터 폴더 지정 (기본: db)
#   --no-index                                 인덱스를 쓰지 않고 항상 선형 탐색 (속도 비교용)

import argparse
import sys
import unicodedata

from executor import Database
from sql_parser import SQLError


# ── 결과 출력 ──

def width(text):
    """터미널에서 차지하는 칸 수 (한글은 2칸)"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, w):
    return text + " " * (w - width(text))


def cell(v):
    return "NULL" if v is None else str(v)


def format_table(columns, rows):
    """
    +----+------+-----+
    | id | name | age |
    +----+------+-----+
    | 1  | 철수 | 20  |
    +----+------+-----+
    """
    cells = [[cell(v) for v in row] for row in rows]
    widths = [max([width(c)] + [width(r[i]) for r in cells]) for i, c in enumerate(columns)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(pad(c, w) for c, w in zip(columns, widths)) + " |", line]
    for r in cells:
        out.append("| " + " | ".join(pad(v, w) for v, w in zip(r, widths)) + " |")
    if cells:
        out.append(line)
    return "\n".join(out)


def print_results(results):
    """결과를 출력하고, 오류가 하나라도 있으면 False"""
    ok = True
    for n, r in enumerate(results, 1):
        if r.kind == "select":
            print(format_table(r.columns, r.rows))
            print(f"({len(r.rows)}개 행)")
            print(f"  실행: {r.plan} · 읽은 행 {r.examined:,}개 · {r.elapsed_ms:,.3f}ms\n")
        elif r.kind == "insert":
            print(r.message + "\n")
        else:
            ok = False
            print(f"오류 [{n}번째 문장] {r.source}")
            print(f"  → {r.message}\n")
    return ok


# ── 실행 방식 3가지 ──

def run_file(db, path):
    try:
        with open(path, encoding="utf-8-sig") as f:      # utf-8-sig: 메모장이 붙이는 BOM도 처리
            sql = f.read()
    except FileNotFoundError:
        print(f"파일이 없어요: {path}")
        return False
    return run_sql(db, sql)


def run_sql(db, sql):
    try:
        results = db.execute(sql)
    except SQLError as e:                                 # 따옴표가 안 닫히는 등 전체를 못 읽는 경우
        print(f"오류: {e}")
        return False
    if not results:
        print("실행할 문장이 없어요")
        return True
    return print_results(results)


def repl(db):
    print("미니 DBMS 대화형 모드 (문장은 ; 로 끝, 종료: exit)")
    buffer = ""
    while True:
        try:
            line = input("mini-dbms> " if not buffer else "        -> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not buffer and line.strip().lower() in ("exit", "quit"):
            break
        buffer += line + "\n"
        if buffer.strip().endswith(";"):                  # ; 가 나올 때까지 여러 줄로 입력 가능
            run_sql(db, buffer)
            buffer = ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="미니 DBMS - SQL 처리기")
    ap.add_argument("file", nargs="?", help="실행할 SQL 파일")
    ap.add_argument("-e", "--execute", help="바로 실행할 SQL")
    ap.add_argument("--db", default="db", help="데이터 폴더 (기본: db)")
    ap.add_argument("--no-index", action="store_true", help="인덱스 끄기 (항상 선형 탐색)")
    args = ap.parse_args(argv)

    try:
        db = Database(args.db, use_index=not args.no_index)
    except SQLError as e:
        print(f"오류: {e}")
        return 1

    if args.execute:
        ok = run_sql(db, args.execute)
    elif args.file:
        ok = run_file(db, args.file)
    else:
        repl(db)
        ok = True
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
