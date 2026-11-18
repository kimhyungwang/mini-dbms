"""Parse the mini DBMS's queries and table commands."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, TypeVar, Union

SQLValue = Union[int, float, str, None]
T = TypeVar("T")


class SQLParseError(ValueError):
    """The input does not match the supported SQL grammar."""


@dataclass(frozen=True)
class InsertStatement:
    table: str
    columns: tuple[str, ...] | None
    values: tuple[SQLValue, ...]
    additional_rows: tuple[tuple[SQLValue, ...], ...] = ()

    @property
    def rows(self) -> tuple[tuple[SQLValue, ...], ...]:
        return (self.values, *self.additional_rows)


@dataclass(frozen=True)
class WhereEquals:
    column: str
    value: SQLValue


@dataclass(frozen=True)
class SelectStatement:
    table: str
    columns: tuple[str, ...] | None  # None represents SELECT *.
    where: WhereEquals | None


@dataclass(frozen=True)
class CreateTableStatement:
    table: str
    columns: tuple[str, ...]
    primary_key: str | None
    auto_primary_index: bool


@dataclass(frozen=True)
class AddPrimaryKeyStatement:
    table: str
    column: str


@dataclass(frozen=True)
class SetAutoPrimaryIndexStatement:
    table: str
    enabled: bool


@dataclass(frozen=True)
class CreateIndexStatement:
    table: str
    name: str
    column: str


Statement = Union[
    InsertStatement,
    SelectStatement,
    CreateTableStatement,
    AddPrimaryKeyStatement,
    SetAutoPrimaryIndexStatement,
    CreateIndexStatement,
]

_KEYWORDS = {"INSERT", "INTO", "VALUES", "SELECT", "FROM", "WHERE", "NULL"}
_DDL_KEYWORDS = {
    "CREATE", "TABLE", "PRIMARY", "KEY", "ALTER", "ADD", "INDEX", "ON", "OFF", "AUTO_PRIMARY_INDEX",
}
_TOKEN_PATTERN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<string>'(?:[^']|'')*')"
    r"|(?P<number>[+-]?[0-9]+(?:\.[0-9]+)?)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<symbol>[(),*=;])"
    r"|(?P<invalid>.)"
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    position: int


def _tokenize(sql: str) -> list[_Token]:
    tokens = []
    for match in _TOKEN_PATTERN.finditer(sql):
        kind = match.lastgroup or "invalid"
        token_text = match.group()
        position = match.start() + 1
        if kind == "space":
            continue
        if kind == "invalid":
            raise SQLParseError(f"Unexpected character {token_text!r} at position {position}")
        if kind == "identifier" and token_text.upper() in _KEYWORDS:
            kind = token_text.upper()
        if kind == "symbol":
            kind = token_text
        tokens.append(_Token(kind, token_text, position))
    tokens.append(_Token("EOF", "", len(sql) + 1))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> Statement:
        statement: Statement
        if self._accept("INSERT"):
            statement = self._parse_insert()
        elif self._accept("SELECT"):
            statement = self._parse_select()
        elif self._accept("CREATE"):
            statement = self._parse_create()
        elif self._accept("ALTER"):
            statement = self._parse_alter()
        else:
            raise self._expected("INSERT, SELECT, CREATE, or ALTER")

        self._accept(";")
        self._expect("EOF")
        return statement

    def _parse_insert(self) -> InsertStatement:
        self._expect("INTO")
        table = self._identifier()
        columns = None
        if self._accept("("):
            columns = self._comma_list(self._identifier)
            self._expect(")")

        self._expect("VALUES")
        rows = self._comma_list(lambda: self._parse_value_row(columns))
        return InsertStatement(table, columns, rows[0], rows[1:])

    def _parse_value_row(self, columns: tuple[str, ...] | None) -> tuple[SQLValue, ...]:
        self._expect("(")
        values = self._comma_list(self._value)
        closing_token = self._expect(")")
        if columns is not None and len(columns) != len(values):
            raise SQLParseError(
                f"Expected {len(columns)} values, got {len(values)} "
                f"at position {closing_token.position}"
            )
        return values

    def _parse_select(self) -> SelectStatement:
        columns = None if self._accept("*") else self._comma_list(self._identifier)
        self._expect("FROM")
        table = self._identifier()
        where = None
        if self._accept("WHERE"):
            column = self._identifier()
            self._expect("=")
            where = WhereEquals(column, self._value())
        return SelectStatement(table, columns, where)

    def _parse_create(self) -> CreateTableStatement | CreateIndexStatement:
        if self._accept("TABLE"):
            table = self._identifier()
            definitions = self._parenthesized_columns()
            primary_keys = [column for column, primary in definitions if primary]
            if len(primary_keys) > 1:
                raise SQLParseError("Only one PRIMARY KEY column is supported")
            enabled = self._parse_on_off() if self._accept("AUTO_PRIMARY_INDEX") else True
            return CreateTableStatement(
                table=table,
                columns=tuple(column for column, _ in definitions),
                primary_key=primary_keys[0] if primary_keys else None,
                auto_primary_index=enabled,
            )

        self._expect("INDEX")
        name = self._identifier()
        self._expect("ON")
        table = self._identifier()
        return CreateIndexStatement(table, name, self._column_in_parentheses())

    def _parenthesized_columns(self) -> tuple[tuple[str, bool], ...]:
        self._expect("(")
        columns = self._comma_list(self._column_definition)
        self._expect(")")
        return columns

    def _column_definition(self) -> tuple[str, bool]:
        column = self._identifier()
        if self._accept("PRIMARY"):
            self._expect("KEY")
            return column, True
        return column, False

    def _parse_alter(self) -> AddPrimaryKeyStatement | SetAutoPrimaryIndexStatement:
        self._expect("TABLE")
        table = self._identifier()
        if self._accept("ADD"):
            self._expect("PRIMARY")
            self._expect("KEY")
            return AddPrimaryKeyStatement(table, self._column_in_parentheses())
        self._expect("AUTO_PRIMARY_INDEX")
        return SetAutoPrimaryIndexStatement(table, self._parse_on_off())

    def _column_in_parentheses(self) -> str:
        self._expect("(")
        column = self._identifier()
        self._expect(")")
        return column

    def _parse_on_off(self) -> bool:
        if self._accept("ON"):
            return True
        self._expect("OFF")
        return False

    def _identifier(self) -> str:
        return self._expect("identifier").text

    def _value(self) -> SQLValue:
        if self._accept("NULL"):
            return None

        string = self._accept("string")
        if string is not None:
            return string.text[1:-1].replace("''", "'")

        number = self._accept("number")
        if number is None:
            raise self._expected("a number, a single-quoted string, or NULL")
        try:
            if "." not in number.text:
                return int(number.text)
            decimal = float(number.text)
            if math.isfinite(decimal):
                return decimal
        except ValueError:
            pass
        raise SQLParseError(f"Number out of range at position {number.position}")

    def _comma_list(self, read_item: Callable[[], T]) -> tuple[T, ...]:
        items = [read_item()]
        while self._accept(","):
            items.append(read_item())
        return tuple(items)

    def _accept(self, kind: str) -> _Token | None:
        token = self._tokens[self._index]
        if token.kind != kind and not (
            token.kind == "identifier" and kind in _DDL_KEYWORDS and token.text.upper() == kind
        ):
            return None
        self._index += 1
        return token

    def _expect(self, kind: str) -> _Token:
        token = self._accept(kind)
        if token is None:
            expected = "end of statement" if kind == "EOF" else kind
            raise self._expected(expected)
        return token

    def _expected(self, expected: str) -> SQLParseError:
        token = self._tokens[self._index]
        actual = "end of statement" if token.kind == "EOF" else repr(token.text)
        return SQLParseError(f"Expected {expected}, got {actual} at position {token.position}")


def parse_sql(sql: str) -> Statement:
    """Parse one statement, with an optional trailing semicolon."""
    return _Parser(_tokenize(sql)).parse()
