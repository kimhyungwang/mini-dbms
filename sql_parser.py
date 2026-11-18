# sql_parser.py
# SQL 글자 → 구조(문장 객체)로 바꾸는 파서
#
# 처리 순서
#   1. 토크나이저: 글자를 의미 있는 조각(토큰)으로 자름
#        "SELECT * FROM users WHERE age > 20;"
#        → [SELECT] [*] [FROM] [users] [WHERE] [age] [>] [20] [;]
#   2. 문장 나누기: ';' 기준으로 토큰 묶음을 나눔 (문자열 안의 ';'은 토큰 하나라서 안 잘림)
#   3. 파서: 토큰 묶음을 읽어서 Insert / Select 객체로 만듦
#        → Select(table="users", columns="*", where=[Condition("age", ">", 20)], limit=None)
#
# 지원 문법
#   INSERT INTO 테이블 [(컬럼, ...)] VALUES (값, ...) [, (값, ...)] ;
#   SELECT * | 컬럼, ... FROM 테이블 [WHERE 컬럼 연산자 값 [AND ...]] [LIMIT 숫자] ;
#   값: 정수(20, -3), 문자열('철수', 'It''s' ← 작은따옴표는 두 번), NULL
#   연산자: =  !=  <>  <  <=  >  >=
#   주석: -- 부터 줄 끝까지

import re
from dataclasses import dataclass, field


class SQLError(Exception):
    """SQL 문법이나 실행에 문제가 있을 때 (사용자에게 보여줄 메시지)"""


# ── 문장 구조 ──

@dataclass
class Condition:
    column: str
    op: str              # "=", "!=", "<", "<=", ">", ">="
    value: object        # int, str, None(NULL)


@dataclass
class Insert:
    table: str
    columns: list        # 컬럼 이름 목록, 생략하면 None
    rows: list           # 행 목록. 행 하나 = 값 목록


@dataclass
class Select:
    table: str
    columns: object      # "*" 또는 컬럼 이름 목록
    where: list = field(default_factory=list)   # Condition 목록 (AND로 연결)
    limit: object = None                        # 정수 또는 None


# ── 1. 토크나이저 ──

@dataclass
class Token:
    kind: str            # "KEYWORD", "IDENT", "NUMBER", "STRING", "SYMBOL"
    value: object
    pos: int             # 원본 글자에서 시작 위치 (오류 메시지용)
    end: int = 0         # 원본 글자에서 끝 위치


KEYWORDS = {"INSERT", "INTO", "VALUES", "SELECT", "FROM", "WHERE", "AND", "LIMIT", "NULL"}

TOKEN_RE = re.compile(r"""
    (?P<SPACE>\s+)
  | (?P<COMMENT>--[^\n]*)
  | (?P<STRING>'(?:[^']|'')*')
  | (?P<NUMBER>-?\d+(?:\.\d+)?)
  | (?P<WORD>[A-Za-z_가-힣][A-Za-z0-9_가-힣]*)
  | (?P<SYMBOL><=|>=|!=|<>|[(),;*=<>])
""", re.VERBOSE)


def tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            if text[pos] == "'":
                raise SQLError(f"작은따옴표가 닫히지 않았어요 (위치 {pos})")
            raise SQLError(f"알 수 없는 문자 '{text[pos]}' (위치 {pos})")
        kind = m.lastgroup
        raw = m.group()
        if kind == "STRING":
            tokens.append(Token("STRING", raw[1:-1].replace("''", "'"), pos, m.end()))
        elif kind == "NUMBER":
            if "." in raw:
                raise SQLError(f"소수는 지원하지 않아요: {raw} (위치 {pos})")
            tokens.append(Token("NUMBER", int(raw), pos, m.end()))
        elif kind == "WORD":
            if raw.upper() in KEYWORDS:
                tokens.append(Token("KEYWORD", raw.upper(), pos, m.end()))   # 키워드는 대소문자 구분 안 함
            else:
                tokens.append(Token("IDENT", raw, pos, m.end()))
        elif kind == "SYMBOL":
            tokens.append(Token("SYMBOL", "!=" if raw == "<>" else raw, pos, m.end()))
        # SPACE, COMMENT는 버림
        pos = m.end()
    return tokens


# ── 2. 문장 나누기 ──

def split_statements(tokens):
    """';' 기준으로 나눔. 빈 문장(;;)은 건너뜀. 마지막 ';'은 생략 가능"""
    statements, current = [], []
    for tok in tokens:
        if tok.kind == "SYMBOL" and tok.value == ";":
            if current:
                statements.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        statements.append(current)
    return statements


# ── 3. 파서 ──

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    # 지금 토큰 보기 / 넘기기
    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def advance(self):
        tok = self.peek()
        self.i += 1
        return tok

    def describe(self, tok):
        return "문장 끝" if tok is None else f"'{tok.value}'"

    def fail(self, expected, tok):
        return SQLError(f"{expected} 와야 해요 (받은 것: {self.describe(tok)})")

    def expect_keyword(self, word):
        tok = self.advance()
        if tok is None or tok.kind != "KEYWORD" or tok.value != word:
            raise self.fail(word + "이(가)", tok)

    def expect_symbol(self, sym):
        tok = self.advance()
        if tok is None or tok.kind != "SYMBOL" or tok.value != sym:
            raise self.fail(f"'{sym}'이(가)", tok)

    def accept_symbol(self, sym):
        tok = self.peek()
        if tok is not None and tok.kind == "SYMBOL" and tok.value == sym:
            self.i += 1
            return True
        return False

    def accept_keyword(self, word):
        tok = self.peek()
        if tok is not None and tok.kind == "KEYWORD" and tok.value == word:
            self.i += 1
            return True
        return False

    def identifier(self, what):
        tok = self.advance()
        if tok is None or tok.kind != "IDENT":
            raise self.fail(f"{what} 이름이", tok)
        return tok.value

    def literal(self):
        tok = self.advance()
        if tok is not None and tok.kind in ("NUMBER", "STRING"):
            return tok.value
        if tok is not None and tok.kind == "KEYWORD" and tok.value == "NULL":
            return None
        raise self.fail("값(숫자, '문자열', NULL)이", tok)

    def end(self):
        tok = self.peek()
        if tok is not None:
            raise SQLError(f"문장이 끝나야 하는데 뒤에 더 있어요 (받은 것: {self.describe(tok)})")

    # 문장 하나
    def statement(self):
        tok = self.peek()
        if tok is not None and tok.kind == "KEYWORD" and tok.value == "INSERT":
            stmt = self.insert()
        elif tok is not None and tok.kind == "KEYWORD" and tok.value == "SELECT":
            stmt = self.select()
        else:
            raise SQLError(f"INSERT 또는 SELECT로 시작해야 해요 (받은 것: {self.describe(tok)})")
        self.end()
        return stmt

    # INSERT INTO 테이블 [(컬럼, ...)] VALUES (값, ...) [, (값, ...)]
    def insert(self):
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.identifier("테이블")
        columns = None
        if self.accept_symbol("("):
            columns = [self.identifier("컬럼")]
            while self.accept_symbol(","):
                columns.append(self.identifier("컬럼"))
            self.expect_symbol(")")
        self.expect_keyword("VALUES")
        rows = [self.value_row()]
        while self.accept_symbol(","):
            rows.append(self.value_row())
        return Insert(table, columns, rows)

    def value_row(self):
        self.expect_symbol("(")
        row = [self.literal()]
        while self.accept_symbol(","):
            row.append(self.literal())
        self.expect_symbol(")")
        return row

    # SELECT * | 컬럼, ... FROM 테이블 [WHERE ...] [LIMIT n]
    def select(self):
        self.expect_keyword("SELECT")
        if self.accept_symbol("*"):
            columns = "*"
        else:
            columns = [self.identifier("컬럼")]
            while self.accept_symbol(","):
                columns.append(self.identifier("컬럼"))
        self.expect_keyword("FROM")
        table = self.identifier("테이블")
        where = []
        if self.accept_keyword("WHERE"):
            where.append(self.condition())
            while self.accept_keyword("AND"):
                where.append(self.condition())
        limit = None
        if self.accept_keyword("LIMIT"):
            tok = self.advance()
            if tok is None or tok.kind != "NUMBER" or tok.value < 0:
                raise self.fail("LIMIT 뒤에는 0 이상의 정수가", tok)
            limit = tok.value
        return Select(table, columns, where, limit)

    def condition(self):
        column = self.identifier("컬럼")
        tok = self.advance()
        if tok is None or tok.kind != "SYMBOL" or tok.value not in ("=", "!=", "<", "<=", ">", ">="):
            raise self.fail("비교 연산자(=, !=, <, <=, >, >=)가", tok)
        return Condition(column, tok.value, self.literal())


def parse(sql):
    """SQL 문장 하나 → Insert / Select"""
    groups = split_statements(tokenize(sql))
    if len(groups) != 1:
        raise SQLError(f"문장이 하나여야 하는데 {len(groups)}개예요")
    return Parser(groups[0]).statement()


def parse_script(text):
    """여러 문장이 든 SQL 글자 → [(문장 객체 또는 SQLError, 원문)]
    한 문장이 틀려도 나머지 문장은 계속 처리할 수 있게 문장마다 따로 파싱"""
    tokens = tokenize(text)                # 따옴표가 안 닫히는 등 전체가 깨진 경우는 여기서 바로 오류
    results = []
    for group in split_statements(tokens):
        source = text[group[0].pos:group[-1].end]   # 오류 메시지에 보여줄 원문
        try:
            results.append((Parser(group).statement(), source))
        except SQLError as e:
            results.append((e, source))
    return results
