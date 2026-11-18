"""Rollback journal for append-only tables on a local Unix filesystem."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO, Iterator

_LOCK_NAME = ".mini_dbms.lock"
# Back up the aligned end of the file in case an append tears its last block.
_RECOVERY_BLOCK_BYTES = 64 * 1024
_FULL_SYNC = getattr(fcntl, "F_FULLFSYNC", None)


class RecoveryError(OSError):
    """A batch could not be recovered or its commit could not be confirmed."""


@contextmanager
def database_lock(directory: Path, *, shared: bool = False) -> Iterator[None]:
    """Allow concurrent readers or give one writer exclusive access."""
    _ensure_directory(directory)
    with (directory / _LOCK_NAME).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def _ensure_directory(directory: Path) -> None:
    if directory.is_dir():
        return
    _ensure_directory(directory.parent)
    directory.mkdir(exist_ok=True)
    _sync_directory(directory.parent)


def sync_file(target: BinaryIO) -> None:
    target.flush()
    if _FULL_SYNC is None:
        os.fsync(target.fileno())
    else:
        fcntl.fcntl(target.fileno(), _FULL_SYNC)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if _FULL_SYNC is not None:
        try:
            lock_file = (directory / _LOCK_NAME).open("r+b")
        except FileNotFoundError:
            return
        with lock_file:
            # Flush the drive after the directory's metadata write.
            sync_file(lock_file)


@contextmanager
def replace_file(path: Path) -> Iterator[BinaryIO]:
    """Publish a complete file using a temporary file in the same directory."""
    target = NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temporary_path = Path(target.name)
    try:
        with target:
            yield target
            sync_file(target)
        temporary_path.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_complete(target: BinaryIO, payload: bytes) -> None:
    if target.write(payload) != len(payload):
        raise OSError("Could not write the complete record")


def _tail_start(size: int) -> int:
    return max(0, (size - 1) // _RECOVERY_BLOCK_BYTES * _RECOVERY_BLOCK_BYTES)


def _snapshot(path: Path) -> tuple[int | None, bytes]:
    try:
        source = path.open("rb")
    except FileNotFoundError:
        return None, b""
    with source:
        size = source.seek(0, os.SEEK_END)
        source.seek(_tail_start(size))
        return size, source.read()


def _checksum(size: int | None, tail: bytes) -> str:
    return sha256(f"{size}\n".encode("ascii") + tail).hexdigest()


@contextmanager
def append_transaction(path: Path) -> Iterator[None]:
    """Publish the undo record before writing, and remove it to commit."""
    journal = path.with_suffix(".journal")
    if journal.exists():
        raise RecoveryError(f"Pending journal for {path.name} must be recovered first")
    size, tail = _snapshot(path)
    record = {"size": size, "tail": tail.hex(), "sha256": _checksum(size, tail)}
    with replace_file(journal) as target:
        write_complete(target, json.dumps(record).encode("ascii"))

    try:
        yield
        # The caller syncs both CSV files. Persist any new file names too.
        _sync_directory(path.parent)
        journal.unlink()
    except BaseException:
        recover_table(path)
        raise

    try:
        _sync_directory(path.parent)
    except OSError as error:
        raise RecoveryError(f"Could not confirm the commit of {path.name}") from error


def recover_table(path: Path) -> bool:
    """Restore the table before use if a pending journal survived a crash."""
    journal = path.with_suffix(".journal")
    try:
        saved = journal.read_bytes()
    except FileNotFoundError:
        return False

    try:
        record = json.loads(saved)
        size = record["size"]
        tail = bytes.fromhex(record["tail"])
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("Invalid original size")
        tail_length = 0 if size is None else size - _tail_start(size)
        if len(tail) != tail_length or record["sha256"] != _checksum(size, tail):
            raise ValueError("Invalid journal contents")
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryError(f"Cannot read {journal.name}") from error

    if size is None:
        path.unlink(missing_ok=True)
    else:
        start = _tail_start(size)
        with path.open("r+b", buffering=0) as target:
            if target.seek(0, os.SEEK_END) < start:
                raise RecoveryError(f"{path.name} is shorter than its recoverable tail")
            target.seek(start)
            write_complete(target, tail)
            target.truncate(size)
            sync_file(target)

    # Indexes are derived from the table and will be rebuilt on load.
    path.with_suffix(".index.csv").unlink(missing_ok=True)
    for index_path in path.parent.glob(f"{path.stem}.*.index.csv"):
        index_path.unlink(missing_ok=True)
    _sync_directory(path.parent)
    journal.unlink()
    _sync_directory(path.parent)
    return True
