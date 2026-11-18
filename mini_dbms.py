"""Execute SQL statements against local table files."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from math import isfinite
from os import SEEK_END, fstat, stat_result
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Iterator, Sequence, TextIO, Union, cast

from bplus_tree import BPlusTree
from sql_parser import (
    AddPrimaryKeyStatement,
    CreateIndexStatement,
    CreateTableStatement,
    InsertStatement,
    SelectStatement,
    SetAutoPrimaryIndexStatement,
    SQLParseError,
    SQLValue,
    Statement,
    parse_sql,
)
from storage_recovery import (
    append_transaction,
    database_lock,
    recover_table,
    replace_file as _replace_file,
    sync_file,
    write_complete as _write_complete,
)


class SQLExecutionError(ValueError):
    """A parsed statement could not be executed."""


class DuplicatePrimaryKeyError(SQLExecutionError):
    """An inserted value conflicts with a primary key."""


@dataclass
class SelectResult:
    columns: tuple[str, ...]
    rows: list[tuple[SQLValue, ...]]


_IndexKey = tuple[int, Union[int, float, str]]
_Index = BPlusTree[_IndexKey, int]
_FileRevision = tuple[int, int, int, int]
_TableRevision = tuple[Union[_FileRevision, None], Union[_FileRevision, None]]
_INDEX_COLUMNS = ("key", "offset", "end_offset")
_CSV_READ_LOCK = Lock()


@dataclass(frozen=True)
class _Schema:
    columns: tuple[str, ...]
    primary_key: str | None = None
    auto_primary_index: bool = True
    manual_indexes: dict[str, str] = field(default_factory=dict)

    @property
    def indexed_columns(self) -> set[str]:
        columns = set(self.manual_indexes.values())
        if self.auto_primary_index and self.primary_key is not None:
            columns.add(self.primary_key)
        return columns


@dataclass
class _Table:
    schema: _Schema
    indexes: dict[str, _Index] = field(default_factory=dict)
    has_data_file: bool = False
    revision: _TableRevision = (None, None)


class MiniDBMS:
    """Store table rows and configured B+ tree index entries in CSV files."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self._data_dir = Path(data_dir)
        self._tables: dict[Path, _Table] = {}
        self._read_initialization = Lock()

    def execute(self, statement: Statement) -> int | SelectResult | str:
        """Return an insert count, selected rows, or a schema command tag."""
        name = statement.table
        if not name.isascii() or not name.isidentifier():
            raise SQLExecutionError(f"Invalid table name {name!r}")
        path = self._data_dir / f"{name.lower()}.csv"

        if isinstance(statement, SelectStatement):
            return self._execute_select(path, statement)
        with database_lock(path.parent):
            try:
                return self._execute_locked(path, statement)
            except BaseException:
                self._tables.pop(path, None)
                raise

    def _execute_select(self, path: Path, statement: SelectStatement) -> SelectResult:
        while True:
            with database_lock(path.parent, shared=True):
                table = self._cached_table_for_read(path)
                if table is not None:
                    return self._select(path, table, statement)

            # Group cold reads so waiting readers can reuse the initialized table
            # without requesting an exclusive lock just to check the cache again.
            with self._read_initialization:
                with database_lock(path.parent, shared=True):
                    if self._cached_table_for_read(path) is not None:
                        continue
                with database_lock(path.parent):
                    if self._prepare_table(path) is None:
                        raise SQLExecutionError(f"Unknown table {statement.table!r}")

    def _cached_table_for_read(self, path: Path) -> _Table | None:
        table = self._tables.get(path)
        if table is not None and not path.with_suffix(".journal").exists():
            if table.revision == _table_revision(path):
                return table
        return None

    def _prepare_table(self, path: Path) -> _Table | None:
        if recover_table(path):
            self._tables.pop(path, None)

        revision = _table_revision(path)
        table = self._tables.get(path)
        if table is None or table.revision != revision:
            self._tables.pop(path, None)
            table = self._load_table(path)
            if table is not None:
                self._tables[path] = table
        return table

    def _execute_locked(self, path: Path, statement: Statement) -> int | str:
        table = self._prepare_table(path)
        if isinstance(statement, InsertStatement):
            return self._insert(path, table, statement)
        if isinstance(statement, CreateTableStatement):
            if table is not None:
                raise SQLExecutionError(f"Table {statement.table!r} already exists")
            columns = _table_columns(tuple(column.lower() for column in statement.columns), path.name)
            primary_key = statement.primary_key.lower() if statement.primary_key is not None else None
            if primary_key is not None:
                _column_position(columns, primary_key)
            schema = _Schema(columns, primary_key, statement.auto_primary_index)
            self._apply_schema(path, None, schema)
            return "CREATE TABLE"
        if table is None:
            raise SQLExecutionError(f"Unknown table {statement.table!r}")
        if isinstance(statement, CreateIndexStatement):
            name, column = statement.name.lower(), statement.column.lower()
            _column_position(table.schema.columns, column)
            manual = table.schema.manual_indexes
            if name in manual or column in manual.values():
                raise SQLExecutionError(f"A manual index named {name!r} or on {column!r} already exists")
            schema = replace(table.schema, manual_indexes={**manual, name: column})
            self._apply_schema(path, table, schema)
            return "CREATE INDEX"
        if isinstance(statement, AddPrimaryKeyStatement):
            if table.schema.primary_key is not None:
                raise SQLExecutionError("The table already has a primary key")
            column = statement.column.lower()
            position = _column_position(table.schema.columns, column)
            keys: set[_IndexKey] = set()
            for value in _column_values(path, table, position):
                _register_primary_key(column, value, keys)
            self._apply_schema(path, table, replace(table.schema, primary_key=column))
            return "ALTER TABLE"
        if isinstance(statement, SetAutoPrimaryIndexStatement):
            if table.schema.auto_primary_index != statement.enabled:
                self._apply_schema(path, table, replace(table.schema, auto_primary_index=statement.enabled))
            return "ALTER TABLE"
        raise SQLExecutionError("Unsupported write statement")

    def _apply_schema(self, path: Path, previous: _Table | None, schema: _Schema) -> None:
        # Prepare derived files before publishing the settings. A failed DDL can
        # leave an unused index file, and the previous settings still load safely.
        table = self._load_table(path, schema)
        assert table is not None
        if previous is not None:
            for column in previous.indexes.keys() - schema.indexed_columns:
                _index_path(path, column).unlink(missing_ok=True)
        _save_schema(path, schema)
        table.revision = _table_revision(path)
        self._tables[path] = table

    def _load_table(self, path: Path, schema: _Schema | None = None) -> _Table | None:
        if schema is None:
            schema = _read_schema(path)
        if schema is None and (not path.exists() or path.stat().st_size == 0):
            _migrate_jsonl(path)
        try:
            source = path.open("rb")
        except FileNotFoundError:
            return _empty_table(path, schema)

        with source:
            header = source.readline()
            if not header:
                return _empty_table(path, schema)
            columns = _table_columns(_decode_csv(header), path.name)
            if schema is None:
                schema = _Schema(columns)
            if schema.columns != columns:
                raise SQLExecutionError(f"Column list does not match the schema of {path.name}")
            data_start = source.tell()
            data_info = fstat(source.fileno())
            indexes = {}
            for column in sorted(schema.indexed_columns):
                index_path = _index_path(path, column)
                index = _load_index(index_path, data_start, data_info.st_size, data_info.st_mtime_ns)
                if index is None:
                    source.seek(data_start)
                    index = _rebuild_index(index_path, source, len(columns), columns.index(column))
                indexes[column] = index
            return _Table(schema, indexes, True, _table_revision(path))

    def _insert(self, path: Path, table: _Table | None, statement: InsertStatement) -> int:
        columns = statement.columns
        if columns is not None:
            columns = tuple(column.lower() for column in columns)
            if len(set(columns)) != len(columns):
                raise SQLExecutionError("INSERT repeats a column name")

        if table is None:
            if not columns:
                raise SQLExecutionError("The first INSERT must name the columns")
            table = _Table(_Schema(columns))

        table_columns = table.schema.columns
        insert_columns = table_columns if columns is None else columns
        for column in insert_columns:
            _column_position(table_columns, column)

        primary_key = table.schema.primary_key
        primary_position = None if primary_key is None else table_columns.index(primary_key)
        primary_keys: set[_IndexKey] = set()
        prepared: list[tuple[tuple[SQLValue, ...], bytes]] = []
        for row_number, values in enumerate(statement.rows, start=1):
            if len(values) != len(insert_columns):
                raise SQLExecutionError(
                    f"Row {row_number}: expected {len(insert_columns)} values, got {len(values)}"
                )
            supplied = dict(zip(insert_columns, values))
            row = tuple(supplied.get(column) for column in table_columns)
            if primary_key is not None and primary_position is not None:
                _register_primary_key(primary_key, row[primary_position], primary_keys)
            try:
                prepared.append((row, _encode_csv_row(row)))
            except ValueError as error:
                raise SQLExecutionError(f"Row {row_number}: {error}") from error

        if primary_key is not None and primary_position is not None:
            _check_existing_keys(path, table, primary_key, primary_position, primary_keys)

        index_positions = {column: table_columns.index(column) for column in table.indexes}
        row_offsets: list[int] = []
        index_mode = "ab" if table.has_data_file else "wb"
        with append_transaction(path), ExitStack() as files:
            target = files.enter_context(path.open("ab"))
            index_files = {
                column: files.enter_context(_index_path(path, column).open(index_mode))
                for column in table.indexes
            }
            offset = target.seek(0, SEEK_END)
            if not table.has_data_file:
                header = _encode_csv(table_columns)
                _write_complete(target, header)
                for index_file in index_files.values():
                    _write_complete(index_file, _encode_csv(_INDEX_COLUMNS))
                offset += len(header)

            for row, record in prepared:
                end_offset = offset + len(record)
                _write_complete(target, record)
                for column, index_file in index_files.items():
                    key = row[index_positions[column]]
                    _write_complete(index_file, _encode_index_record(key, offset, end_offset))
                row_offsets.append(offset)
                offset = end_offset

            sync_file(target)
            for index_file in index_files.values():
                sync_file(index_file)

        for (row, _), offset in zip(prepared, row_offsets):
            for column, index in table.indexes.items():
                key = row[index_positions[column]]
                if key is not None:
                    index.insert(_index_key(key), offset)
        table.has_data_file = True
        table.revision = _table_revision(path)
        self._tables[path] = table
        return len(prepared)

    def _select(self, path: Path, table: _Table, statement: SelectStatement) -> SelectResult:
        table_columns = table.schema.columns
        columns = table_columns
        if statement.columns is not None:
            columns = tuple(column.lower() for column in statement.columns)
        positions = [_column_position(table_columns, column) for column in columns]
        rows: list[tuple[SQLValue, ...]] = []
        where = statement.where
        filter_position = None
        filter_value = None
        index = None
        if where is not None:
            column = where.column.lower()
            filter_position = _column_position(table_columns, column)
            filter_value = where.value
            index = table.indexes.get(column)
            if filter_value is None:
                return SelectResult(columns, rows)
        if not table.has_data_file:
            return SelectResult(columns, rows)

        with path.open("rb") as source:
            source.readline()  # The first line describes the table's columns.
            records: Iterator[bytes] = iter(source)
            if index is not None and filter_value is not None:
                offsets = index.search(_index_key(filter_value))
                records = _indexed_records(source, offsets)

            for record in records:
                row = _decode_row(record, len(table_columns))
                if filter_position is None or row[filter_position] == filter_value:
                    rows.append(tuple(row[position] for position in positions))
        return SelectResult(columns, rows)


def _file_revision(info: stat_result) -> _FileRevision:
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _path_revision(path: Path) -> _FileRevision | None:
    try:
        return _file_revision(path.stat())
    except FileNotFoundError:
        return None


def _table_revision(path: Path) -> _TableRevision:
    return _path_revision(path), _path_revision(path.with_suffix(".schema.json"))


def _index_path(path: Path, column: str) -> Path:
    return path.with_suffix(f".{column}.index.csv")


def _read_schema(path: Path) -> _Schema | None:
    schema_path = path.with_suffix(".schema.json")
    try:
        record = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise SQLExecutionError(f"Invalid schema in {schema_path.name}") from error

    if not isinstance(record, dict) or not isinstance(record.get("columns"), list):
        raise SQLExecutionError(f"Invalid schema in {schema_path.name}")
    columns = _table_columns(record["columns"], schema_path.name)
    primary = record.get("primary_key")
    automatic = record.get("auto_primary_index")
    manual = record.get("manual_indexes")
    if primary is not None and (not isinstance(primary, str) or primary not in columns):
        raise SQLExecutionError(f"Invalid primary key in {schema_path.name}")
    if not isinstance(automatic, bool) or not isinstance(manual, dict):
        raise SQLExecutionError(f"Invalid index settings in {schema_path.name}")
    for name, column in manual.items():
        if not name.isascii() or not name.isidentifier() or not isinstance(column, str) or column not in columns:
            raise SQLExecutionError(f"Invalid manual index in {schema_path.name}")
    return _Schema(columns, primary, automatic, manual)


def _save_schema(path: Path, schema: _Schema) -> None:
    payload = json.dumps(asdict(schema), indent=2).encode("utf-8") + b"\n"
    with _replace_file(path.with_suffix(".schema.json")) as target:
        _write_complete(target, payload)


def _empty_table(path: Path, schema: _Schema | None) -> _Table | None:
    if schema is None:
        return None
    indexes = {column: BPlusTree() for column in sorted(schema.indexed_columns)}
    return _Table(schema, indexes, revision=_table_revision(path))


def _column_values(path: Path, table: _Table, position: int) -> Iterator[SQLValue]:
    if not table.has_data_file:
        return
    with path.open("rb") as source:
        source.readline()
        for record in source:
            yield _decode_row(record, len(table.schema.columns))[position]


def _register_primary_key(column: str, value: SQLValue, keys: set[_IndexKey]) -> None:
    if value is None:
        raise SQLExecutionError(f"Primary key {column!r} cannot be NULL")
    key = _index_key(value)
    if key in keys:
        raise DuplicatePrimaryKeyError(f"Duplicate primary key {column!r}: {value!r}")
    keys.add(key)


def _check_existing_keys(
    path: Path, table: _Table, column: str, position: int, keys: set[_IndexKey]
) -> None:
    index = table.indexes.get(column)
    if index is not None:
        for key in keys:
            if index.search(key):
                raise DuplicatePrimaryKeyError(f"Duplicate primary key {column!r}: {key[1]!r}")
        return

    for value in _column_values(path, table, position):
        if value is not None and _index_key(value) in keys:
            raise DuplicatePrimaryKeyError(f"Duplicate primary key {column!r}: {value!r}")


def _index_key(value: int | float | str) -> _IndexKey:
    # Numeric keys compare with each other; text keys sort separately.
    return (1, value) if isinstance(value, str) else (0, value)


def _column_position(columns: tuple[str, ...], column: str) -> int:
    try:
        return columns.index(column)
    except ValueError as error:
        raise SQLExecutionError(f"Unknown column {column!r}") from error


def _encode_csv(fields: Sequence[str | int]) -> bytes:
    text = io.StringIO(newline="")
    csv.writer(text, lineterminator="\n").writerow(fields)
    return text.getvalue().encode("utf-8")


def _decode_csv(line: bytes) -> list[str]:
    if not line.endswith(b"\n"):
        raise SQLExecutionError("Incomplete record in the table file")
    try:
        text = line.decode("utf-8")
        with _CSV_READ_LOCK:
            limit = csv.field_size_limit()
            if len(text) <= limit:
                return next(csv.reader([text], strict=True))
            # The CSV field limit is process-wide, including concurrent readers.
            csv.field_size_limit(len(text))
            try:
                return next(csv.reader([text], strict=True))
            finally:
                csv.field_size_limit(limit)
    except (UnicodeDecodeError, csv.Error) as error:
        raise SQLExecutionError("Invalid CSV record") from error


def _encode_value(value: SQLValue) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _encode_csv_row(values: Sequence[SQLValue]) -> bytes:
    # Encode each scalar so that "3", 3, and NULL round-trip as distinct values.
    return _encode_csv([_encode_value(value) for value in values])


def _validate_values(values: object) -> list[SQLValue]:
    if not isinstance(values, list):
        raise SQLExecutionError("Expected a list in the table file")
    for value in values:
        if value is not None and type(value) not in (int, float, str):
            raise SQLExecutionError("Invalid value type in the table file")
        if isinstance(value, float) and not isfinite(value):
            raise SQLExecutionError("Non-finite number in the table file")
    return values


def _decode_row(line: bytes, column_count: int) -> list[SQLValue]:
    fields = _decode_csv(line)
    try:
        decoded = [json.loads(field) for field in fields]
    except ValueError as error:
        raise SQLExecutionError("Invalid value in the CSV row") from error
    values = _validate_values(decoded)
    if len(values) != column_count:
        raise SQLExecutionError("Invalid number of values in the table file")
    return values


def _table_columns(values: Sequence[SQLValue], filename: str) -> tuple[str, ...]:
    if not values or not all(isinstance(value, str) for value in values):
        raise SQLExecutionError(f"Invalid column list in {filename}")
    columns = cast(tuple[str, ...], tuple(values))
    if any(not column.isascii() or not column.isidentifier() for column in columns):
        raise SQLExecutionError(f"Invalid column name in {filename}")
    if len(set(columns)) != len(columns):
        raise SQLExecutionError(f"Repeated columns in {filename}")
    return columns


def _encode_index_record(value: SQLValue, offset: int, end_offset: int) -> bytes:
    return _encode_csv((_encode_value(value), offset, end_offset))


def _load_index(path: Path, data_start: int, data_end: int, data_mtime_ns: int) -> _Index | None:
    index = BPlusTree()
    expected_offset = data_start
    try:
        with path.open("rb") as source:
            if fstat(source.fileno()).st_mtime_ns < data_mtime_ns:
                return None
            if tuple(_decode_csv(source.readline())) != _INDEX_COLUMNS:
                return None
            for line in source:
                key, start_text, end_text = _decode_csv(line)
                offset, end_offset = int(start_text), int(end_text)
                if offset != expected_offset or not offset < end_offset <= data_end:
                    return None
                value = _validate_values([json.loads(key)])[0]
                if value is not None:
                    index.insert(_index_key(value), offset)
                expected_offset = end_offset
    except (FileNotFoundError, ValueError):
        return None
    # This also catches a data append that finished before its index append.
    return index if expected_offset == data_end else None


def _rebuild_index(path: Path, source: BinaryIO, column_count: int, position: int) -> _Index:
    index = BPlusTree()
    offset = source.tell()
    with _replace_file(path) as target:
        _write_complete(target, _encode_csv(_INDEX_COLUMNS))
        while line := source.readline():
            value = _decode_row(line, column_count)[position]
            end_offset = source.tell()
            _write_complete(target, _encode_index_record(value, offset, end_offset))
            if value is not None:
                index.insert(_index_key(value), offset)
            offset = end_offset
    return index


def _migrate_jsonl(path: Path) -> None:
    legacy = path.with_suffix(".jsonl")
    try:
        source = legacy.open("rb")
    except FileNotFoundError:
        return
    with source:
        header = source.readline()
        if not header:
            return
        columns = _table_columns(_decode_jsonl(header), legacy.name)
        with _replace_file(path) as target:
            _write_complete(target, _encode_csv(columns))
            for line in source:
                values = _decode_jsonl(line)
                if len(values) != len(columns):
                    raise SQLExecutionError(f"Invalid number of values in {legacy.name}")
                _write_complete(target, _encode_csv_row(values))


def _decode_jsonl(line: bytes) -> list[SQLValue]:
    if not line.endswith(b"\n"):
        raise SQLExecutionError("Incomplete record in the JSONL table file")
    try:
        values = json.loads(line)
    except ValueError as error:
        raise SQLExecutionError("Invalid JSON in the table file") from error
    return _validate_values(values)


def _indexed_records(source: BinaryIO, offsets: list[int]) -> Iterator[bytes]:
    for offset in offsets:
        source.seek(offset)
        yield source.readline()


def _read_statements(source: TextIO) -> Iterator[str]:
    parts: list[str] = []
    in_string = False
    interactive = source.isatty()
    while True:
        if interactive:
            print("...> " if parts else "sql> ", end="", flush=True)
        line = source.readline()
        if not line:
            if parts:
                yield "".join(parts)
            return

        start = 0
        for index, character in enumerate(line):
            if character == "'":
                # SQL's escaped quote ('') toggles twice and stays inside the string.
                in_string = not in_string
            elif character == ";" and not in_string:
                parts.append(line[start:index + 1])
                yield "".join(parts)
                parts.clear()
                start = index + 1

        tail = line[start:]
        if parts or tail.strip():
            parts.append(tail)


def main() -> int:
    argument_parser = argparse.ArgumentParser(description="Run the mini DBMS SQL console.")
    argument_parser.add_argument("--data-dir", default="data", help="directory for table files")
    args = argument_parser.parse_args()
    database = MiniDBMS(args.data_dir)
    failed = False

    for sql in _read_statements(sys.stdin):
        try:
            result = database.execute(parse_sql(sql))
        except (SQLParseError, SQLExecutionError, OSError) as error:
            print(f"Error: {error}", file=sys.stderr)
            failed = True
            continue

        if isinstance(result, SelectResult):
            print(json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False))
        elif isinstance(result, int):
            print(f"INSERT {result}")
        else:
            print(result)

    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
