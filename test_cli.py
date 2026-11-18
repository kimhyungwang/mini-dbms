# test_cli.py — 기능 테스트: 실제로 터미널 명령(python main.py ...)을 실행해서 확인
# 실행: python -m unittest -v

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "main.py")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "db")
        os.mkdir(self.db)
        with open(os.path.join(self.db, "schema.json"), "w", encoding="utf-8") as f:
            json.dump({"users": {"name": "TEXT", "age": "INT"}}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args, stdin=None):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, MAIN, "--db", self.db, *args], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=env)
        return p.returncode, p.stdout

    def write_sql(self, text, name="q.sql"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_run_sql_file(self):
        path = self.write_sql("INSERT INTO users VALUES ('철수', 20);\nSELECT * FROM users;")
        code, out = self.run_cli(path)
        self.assertEqual(code, 0)
        self.assertIn("1개 행 추가 (id = 1)", out)
        self.assertIn("| 1  | 철수 | 20  |", out)       # 한글 폭까지 맞춘 표
        self.assertIn("(1개 행)", out)

    def test_data_persists_between_runs(self):
        self.run_cli("-e", "INSERT INTO users VALUES ('a', 1)")
        code, out = self.run_cli("-e", "SELECT name FROM users")    # 프로그램을 새로 실행
        self.assertIn("| a    |", out)

    def test_execute_option(self):
        code, out = self.run_cli("-e", "SELECT * FROM users")
        self.assertEqual(code, 0)
        self.assertIn("(0개 행)", out)

    def test_errors_reported_and_exit_code_1(self):
        path = self.write_sql("SELECT * FROM nope;\nINSERT INTO users VALUES ('a', 1);")
        code, out = self.run_cli(path)
        self.assertEqual(code, 1)
        self.assertIn("오류 [1번째 문장] SELECT * FROM nope", out)
        self.assertIn("1개 행 추가", out)                 # 오류 뒤 문장도 실행됨

    def test_missing_file(self):
        code, out = self.run_cli(os.path.join(self.dir, "없는파일.sql"))
        self.assertEqual(code, 1)
        self.assertIn("파일이 없어요", out)

    def test_file_with_bom(self):
        path = os.path.join(self.dir, "bom.sql")
        with open(path, "w", encoding="utf-8-sig") as f:   # 윈도우 메모장 형식
            f.write("SELECT * FROM users;")
        self.assertEqual(self.run_cli(path)[0], 0)

    def test_empty_file(self):
        code, out = self.run_cli(self.write_sql("-- 주석만\n"))
        self.assertEqual(code, 0)
        self.assertIn("실행할 문장이 없어요", out)

    def test_unclosed_quote_whole_file(self):
        code, out = self.run_cli(self.write_sql("INSERT INTO users VALUES ('abc, 1);"))
        self.assertEqual(code, 1)
        self.assertIn("작은따옴표가 닫히지 않았어요", out)

    def test_interactive_mode_multiline(self):
        code, out = self.run_cli(stdin="INSERT INTO users\nVALUES ('a', 1);\nSELECT * FROM users;\nexit\n")
        self.assertEqual(code, 0)
        self.assertIn("1개 행 추가", out)
        self.assertIn("(1개 행)", out)

    def test_plan_shown_and_no_index_option(self):
        self.run_cli("-e", "INSERT INTO users VALUES ('a', 1), ('b', 2)")
        code, out = self.run_cli("-e", "SELECT * FROM users WHERE id = 2")
        self.assertIn("실행: B+ 트리 인덱스", out)
        self.assertIn("읽은 행 1개", out)
        code, out = self.run_cli("--no-index", "-e", "SELECT * FROM users WHERE id = 2")
        self.assertIn("실행: 선형 탐색", out)
        self.assertIn("읽은 행 2개", out)

    def test_missing_schema(self):
        p = subprocess.run([sys.executable, MAIN, "--db", os.path.join(self.dir, "none"), "-e", "SELECT * FROM t"],
                           capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("스키마 파일이 없어요", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
