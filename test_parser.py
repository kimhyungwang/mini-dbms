# test_parser.py — 파서 단위 테스트
# 실행: python -m unittest -v

import unittest

from sql_parser import Condition, Insert, Select, SQLError, parse, parse_script, tokenize


class TokenizeTest(unittest.TestCase):
    def test_basic_tokens(self):
        kinds = [(t.kind, t.value) for t in tokenize("SELECT * FROM users WHERE age >= 20;")]
        self.assertEqual(kinds, [
            ("KEYWORD", "SELECT"), ("SYMBOL", "*"), ("KEYWORD", "FROM"), ("IDENT", "users"),
            ("KEYWORD", "WHERE"), ("IDENT", "age"), ("SYMBOL", ">="), ("NUMBER", 20), ("SYMBOL", ";"),
        ])

    def test_keywords_case_insensitive(self):
        self.assertEqual(tokenize("select")[0].value, "SELECT")

    def test_string_with_escaped_quote_and_semicolon(self):
        tok = tokenize("'It''s; ok'")[0]
        self.assertEqual((tok.kind, tok.value), ("STRING", "It's; ok"))

    def test_comment_ignored(self):
        self.assertEqual(len(tokenize("-- 주석\nSELECT")), 1)

    def test_not_equal_alias(self):
        self.assertEqual(tokenize("<>")[0].value, "!=")

    def test_unclosed_quote(self):
        with self.assertRaises(SQLError):
            tokenize("'abc")

    def test_unknown_char(self):
        with self.assertRaises(SQLError):
            tokenize("SELECT @")

    def test_decimal_rejected(self):
        with self.assertRaises(SQLError):
            tokenize("1.5")


class ParseInsertTest(unittest.TestCase):
    def test_insert_all_columns(self):
        self.assertEqual(parse("INSERT INTO users VALUES ('철수', 20)"),
                         Insert("users", None, [["철수", 20]]))

    def test_insert_with_columns_and_multi_rows(self):
        self.assertEqual(parse("INSERT INTO users (name, age) VALUES ('a', 1), ('b', NULL);"),
                         Insert("users", ["name", "age"], [["a", 1], ["b", None]]))

    def test_negative_number(self):
        self.assertEqual(parse("INSERT INTO t VALUES (-5)").rows, [[-5]])

    def test_insert_errors(self):
        for sql in ["INSERT users VALUES (1)", "INSERT INTO users VALUES", "INSERT INTO users VALUES (1",
                    "INSERT INTO users VALUES ()", "INSERT INTO users (name VALUES (1)"]:
            with self.subTest(sql=sql), self.assertRaises(SQLError):
                parse(sql)


class ParseSelectTest(unittest.TestCase):
    def test_select_star(self):
        self.assertEqual(parse("SELECT * FROM users"), Select("users", "*"))

    def test_select_columns_where_limit(self):
        self.assertEqual(
            parse("SELECT name, age FROM users WHERE age > 18 AND name != 'x' LIMIT 3"),
            Select("users", ["name", "age"],
                   [Condition("age", ">", 18), Condition("name", "!=", "x")], 3))

    def test_select_errors(self):
        for sql in ["SELECT FROM users", "SELECT * users", "SELECT * FROM", "SELECT * FROM t WHERE",
                    "SELECT * FROM t WHERE a 1", "SELECT * FROM t WHERE a = ", "SELECT * FROM t LIMIT -1",
                    "SELECT * FROM t LIMIT 'a'", "SELECT * FROM t extra", "DELETE FROM t"]:
            with self.subTest(sql=sql), self.assertRaises(SQLError):
                parse(sql)


class ParseScriptTest(unittest.TestCase):
    def test_multiple_statements(self):
        results = parse_script("INSERT INTO t VALUES (1); SELECT * FROM t;")
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0][0], Insert)
        self.assertIsInstance(results[1][0], Select)

    def test_bad_statement_does_not_stop_others(self):
        results = parse_script("SELEC x; SELECT * FROM t")
        self.assertIsInstance(results[0][0], SQLError)
        self.assertIsInstance(results[1][0], Select)

    def test_source_text_kept(self):
        self.assertEqual(parse_script("  SELECT * FROM t ;")[0][1], "SELECT * FROM t")

    def test_empty_statements_skipped(self):
        self.assertEqual(parse_script(";; -- 주석만\n;"), [])

    def test_semicolon_inside_string_not_split(self):
        self.assertEqual(len(parse_script("INSERT INTO t VALUES ('a;b'); SELECT * FROM t")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
