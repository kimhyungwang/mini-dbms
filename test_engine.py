# test_engine.py — 저장소(storage) + 실행기(executor) 단위 테스트
# 실행: python -m unittest -v

import json
import os
import shutil
import tempfile
import unittest

from executor import Database
from sql_parser import SQLError
from storage import Table

SCHEMA = {"users": {"name": "TEXT", "age": "INT"}, "products": {"name": "TEXT", "price": "INT"}}


def make_db_dir(schema=SCHEMA):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "schema.json"), "w", encoding="utf-8") as f:
        json.dump(schema, f)
    return d


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cols = [("name", "TEXT"), ("age", "INT")]

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_insert_assigns_increasing_ids(self):
        t = Table(self.dir, "users", self.cols)
        self.assertEqual(t.insert([["a", 1], ["b", 2]]), [1, 2])
        self.assertEqual(t.insert([["c", 3]]), [3])

    def test_scan_returns_rows_in_order(self):
        t = Table(self.dir, "users", self.cols)
        t.insert([["철수", 20], ["영희", None]])
        self.assertEqual(list(t.scan()), [[1, "철수", 20], [2, "영희", None]])

    def test_data_survives_reopen(self):
        Table(self.dir, "users", self.cols).insert([["a", 1]])
        t = Table(self.dir, "users", self.cols)          # 프로그램을 다시 켠 것과 같음
        self.assertEqual(list(t.scan()), [[1, "a", 1]])
        self.assertEqual(t.insert([["b", 2]]), [2])      # id가 1부터 다시 시작하지 않음

    def test_next_id_recovered_without_meta(self):
        Table(self.dir, "users", self.cols).insert([["a", 1], ["b", 2]])
        os.remove(os.path.join(self.dir, "users.meta.json"))
        self.assertEqual(Table(self.dir, "users", self.cols).next_id, 3)

    def test_special_characters_stored_safely(self):
        t = Table(self.dir, "users", self.cols)
        t.insert([['쉼표, "따옴표", 줄\n바꿈', 1]])
        self.assertEqual(list(t.scan())[0][1], '쉼표, "따옴표", 줄\n바꿈')

    def test_broken_line_skipped(self):
        t = Table(self.dir, "users", self.cols)
        t.insert([["a", 1]])
        with open(t.path, "a", encoding="utf-8") as f:
            f.write('[2, "반쯤 써진')                     # 꺼지는 순간 반쯤 써진 줄
        self.assertEqual(len(list(t.scan())), 1)

    def test_empty_table_scan(self):
        self.assertEqual(list(Table(self.dir, "users", self.cols).scan()), [])


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.dir = make_db_dir()
        self.db = Database(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def one(self, sql):
        results = self.db.execute(sql)
        self.assertEqual(len(results), 1)
        return results[0]

    def rows(self, sql):
        r = self.one(sql)
        self.assertEqual(r.kind, "select", r.message)
        return r.rows

    def error(self, sql):
        r = self.one(sql)
        self.assertEqual(r.kind, "error")
        return r.message

    # ── INSERT ──
    def test_insert_and_select(self):
        self.assertEqual(self.one("INSERT INTO users VALUES ('철수', 20)").message, "1개 행 추가 (id = 1)")
        self.assertEqual(self.rows("SELECT * FROM users"), [[1, "철수", 20]])

    def test_insert_multi_rows_message(self):
        self.assertEqual(self.one("INSERT INTO users VALUES ('a', 1), ('b', 2)").message,
                         "2개 행 추가 (id = 1 ~ 2)")

    def test_insert_with_column_list_missing_is_null(self):
        self.one("INSERT INTO users (age) VALUES (30)")
        self.assertEqual(self.rows("SELECT * FROM users"), [[1, None, 30]])

    def test_insert_column_order_any(self):
        self.one("INSERT INTO users (age, name) VALUES (30, 'a')")
        self.assertEqual(self.rows("SELECT name, age FROM users"), [["a", 30]])

    def test_insert_errors(self):
        self.assertIn("테이블이 없어요", self.error("INSERT INTO nope VALUES (1)"))
        self.assertIn("컬럼이 없어요", self.error("INSERT INTO users (foo) VALUES (1)"))
        self.assertIn("직접 넣을 수 없어요", self.error("INSERT INTO users (id, name) VALUES (1, 'a')"))
        self.assertIn("두 번", self.error("INSERT INTO users (name, name) VALUES ('a', 'b')"))
        self.assertIn("값은 1개", self.error("INSERT INTO users VALUES ('a')"))
        self.assertIn("INT 컬럼", self.error("INSERT INTO users VALUES ('a', 'twenty')"))
        self.assertIn("TEXT 컬럼", self.error("INSERT INTO users VALUES (1, 2)"))

    def test_failed_multi_row_insert_saves_nothing(self):
        self.error("INSERT INTO users VALUES ('a', 1), ('b', 'bad')")
        self.assertEqual(self.rows("SELECT * FROM users"), [])     # 앞 행도 저장 안 됨

    # ── SELECT ──
    def fill(self):
        self.db.execute("INSERT INTO users VALUES ('철수', 20), ('영희', 22), ('민수', 17), ('지수', NULL)")

    def test_select_columns(self):
        self.fill()
        self.assertEqual(self.rows("SELECT name FROM users"), [["철수"], ["영희"], ["민수"], ["지수"]])

    def test_where_operators(self):
        self.fill()
        cases = {
            "age = 20": [1], "age != 20": [2, 3], "age < 20": [3], "age <= 20": [1, 3],
            "age > 20": [2], "age >= 20": [1, 2], "id = 4": [4], "name = '영희'": [2],
        }
        for cond, ids in cases.items():
            with self.subTest(cond=cond):
                self.assertEqual([r[0] for r in self.rows(f"SELECT * FROM users WHERE {cond}")], ids)

    def test_where_and(self):
        self.fill()
        self.assertEqual(self.rows("SELECT id FROM users WHERE age >= 18 AND name != '철수'"), [[2]])

    def test_null_never_matches(self):
        self.fill()
        self.assertEqual(self.rows("SELECT * FROM users WHERE age = NULL"), [])
        self.assertEqual([r[0] for r in self.rows("SELECT * FROM users WHERE age != 0")], [1, 2, 3])

    def test_limit(self):
        self.fill()
        self.assertEqual(len(self.rows("SELECT * FROM users LIMIT 2")), 2)
        self.assertEqual(self.rows("SELECT * FROM users LIMIT 0"), [])
        self.assertEqual(len(self.rows("SELECT * FROM users LIMIT 100")), 4)

    def test_no_match_returns_empty(self):
        self.fill()
        self.assertEqual(self.rows("SELECT * FROM users WHERE id = 999"), [])

    def test_select_errors(self):
        self.assertIn("테이블이 없어요", self.error("SELECT * FROM nope"))
        self.assertIn("컬럼이 없어요", self.error("SELECT foo FROM users"))
        self.assertIn("컬럼이 없어요", self.error("SELECT * FROM users WHERE foo = 1"))
        self.assertIn("INT 컬럼", self.error("SELECT * FROM users WHERE age = '20'"))

    # ── 여러 문장 ──
    def test_error_does_not_stop_script(self):
        results = self.db.execute("INSERT INTO users VALUES ('a', 1); SELECT * FROM nope; SELECT * FROM users;")
        self.assertEqual([r.kind for r in results], ["insert", "error", "select"])

    def test_tables_are_separate_files(self):
        self.db.execute("INSERT INTO users VALUES ('a', 1); INSERT INTO products VALUES ('키보드', 50000);")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "users.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "products.jsonl")))
        self.assertEqual(self.rows("SELECT * FROM products"), [[1, "키보드", 50000]])

    # ── 스키마 ──
    def test_missing_schema(self):
        with self.assertRaises(SQLError):
            Database(tempfile.mkdtemp())

    def test_invalid_schema_type(self):
        with self.assertRaises(SQLError):
            Database(make_db_dir({"t": {"x": "FLOAT"}}))

    def test_schema_with_id_rejected(self):
        with self.assertRaises(SQLError):
            Database(make_db_dir({"t": {"id": "INT"}}))



class StorageIndexTest(unittest.TestCase):
    """[Phase 2] 저장소의 B+ 트리 인덱스 (id → 파일 위치)"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cols = [("name", "TEXT"), ("age", "INT")]

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_index_points_to_line_start(self):
        t = Table(self.dir, "users", self.cols)
        t.insert([["철수", 1], ["영희", 2], ["민수", 3]])     # 한글 = 여러 바이트
        for row_id in (1, 2, 3):
            offset = t.index.search(row_id)
            self.assertEqual(next(t.read_at([offset]))[0], row_id)

    def test_index_rebuilt_on_reopen(self):
        Table(self.dir, "users", self.cols).insert([["a", 1], ["b", 2]])
        t = Table(self.dir, "users", self.cols)              # 다시 켜면 파일을 훑어서 다시 만듦
        self.assertEqual(t.row_count, 2)
        self.assertEqual(next(t.read_at([t.index.search(2)])), [2, "b", 2])

    def test_insert_after_broken_line(self):
        t = Table(self.dir, "users", self.cols)
        t.insert([["a", 1]])
        with open(t.path, "a", encoding="utf-8") as f:
            f.write('[2, "반쯤 써진')                         # 꺼지는 순간 반쯤 써진 줄
        t = Table(self.dir, "users", self.cols)
        self.assertEqual(t.row_count, 1)                      # 깨진 줄은 인덱스에 안 들어감
        new_id = t.insert([["b", 2]])[0]
        self.assertEqual(next(t.read_at([t.index.search(new_id)]))[1], "b")   # 깨진 줄 뒤에 붙지 않음
        self.assertEqual([r[1] for r in t.scan()], ["a", "b"])


class IndexQueryTest(unittest.TestCase):
    """[Phase 2] WHERE id 조건일 때 인덱스를 쓰는지, 결과가 선형 탐색과 같은지"""

    def setUp(self):
        self.dir = make_db_dir()
        self.db = Database(self.dir)
        values = ", ".join(f"('u{i}', {i % 10})" for i in range(1, 501))
        self.db.execute(f"INSERT INTO users VALUES {values}")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_sql(self, sql, use_index=True):
        r = self.db.execute(sql, use_index=use_index)[0]
        self.assertEqual(r.kind, "select", r.message)
        return r

    def test_id_equal_uses_index_and_reads_one_row(self):
        r = self.run_sql("SELECT * FROM users WHERE id = 250")
        self.assertEqual(r.rows, [[250, "u250", 0]])
        self.assertIn("B+ 트리", r.plan)
        self.assertEqual(r.examined, 1)

    def test_non_id_condition_scans(self):
        r = self.run_sql("SELECT * FROM users WHERE name = 'u250'")
        self.assertIn("선형 탐색", r.plan)
        self.assertEqual(r.examined, 500)

    def test_index_and_scan_give_same_results(self):
        for where in ["id = 1", "id = 500", "id = 999", "id > 495", "id >= 495", "id < 4", "id <= 4",
                      "id >= 100 AND id < 110", "id > 10 AND id <= 12 AND age != 1",
                      "id = 7 AND name = 'u7'", "id = 7 AND name = 'x'", "id > 5 AND id < 3",
                      "id >= 5 AND id <= 5", "id > 5 AND id < 5", "id = NULL", "id = 3 AND id = 4"]:
            with self.subTest(where=where):
                sql = f"SELECT * FROM users WHERE {where}"
                self.assertEqual(self.run_sql(sql).rows, self.run_sql(sql, use_index=False).rows)

    def test_range_reads_only_needed_rows(self):
        r = self.run_sql("SELECT id FROM users WHERE id >= 100 AND id < 110")
        self.assertEqual([row[0] for row in r.rows], list(range(100, 110)))
        self.assertEqual(r.examined, 10)
        self.assertIn("범위", r.plan)

    def test_impossible_range_reads_nothing(self):
        r = self.run_sql("SELECT * FROM users WHERE id > 5 AND id < 3")
        self.assertEqual((r.rows, r.examined), ([], 0))

    def test_not_equal_on_id_scans(self):
        self.assertIn("선형 탐색", self.run_sql("SELECT * FROM users WHERE id != 1").plan)

    def test_limit_with_index_range(self):
        r = self.run_sql("SELECT id FROM users WHERE id > 10 LIMIT 3")
        self.assertEqual(r.rows, [[11], [12], [13]])
        self.assertEqual(r.examined, 3)

    def test_new_insert_is_indexed(self):
        self.db.execute("INSERT INTO users VALUES ('new', 1)")
        r = self.run_sql("SELECT name FROM users WHERE id = 501")
        self.assertEqual((r.rows, r.examined), ([["new"]], 1))

    def test_index_survives_reopen(self):
        db = Database(self.dir)
        r = db.execute("SELECT name FROM users WHERE id = 321")[0]
        self.assertEqual((r.rows, r.examined), ([["u321"]], 1))

    def test_empty_table_with_index(self):
        r = self.run_sql("SELECT * FROM products WHERE id = 1")
        self.assertEqual(r.rows, [])

    def test_use_index_default_off(self):
        db = Database(self.dir, use_index=False)
        self.assertIn("선형 탐색", db.execute("SELECT * FROM users WHERE id = 1")[0].plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
