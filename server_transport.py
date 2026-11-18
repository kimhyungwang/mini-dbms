"""Length-prefixed TCP messages and a bounded pool of connection workers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from socket import socket
from threading import BoundedSemaphore

MAX_FRAME_BYTES = 16 * 1024 * 1024


class ProtocolError(ValueError):
    """An incomplete or invalid TCP message."""


def _receive_exact(connection: socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(min(65536, size - len(data)))
        if not chunk:
            raise ProtocolError("Connection closed before the message was complete")
        data.extend(chunk)
    return bytes(data)


def receive_message(connection: socket) -> dict:
    size = int.from_bytes(_receive_exact(connection, 4), "big")
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ProtocolError("Invalid message length")
    body = _receive_exact(connection, size)
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise ProtocolError("Invalid UTF-8 JSON message") from error
    if not isinstance(message, dict):
        raise ProtocolError("Expected a JSON object")
    return message


def send_message(connection: socket, message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError("Message exceeds the size limit")
    connection.sendall(len(body).to_bytes(4, "big") + body)


class WorkerPoolMixIn:
    """Dispatch accepted connections to a fixed number of worker threads."""

    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, *args, workers: int, **kwargs) -> None:
        self._workers = ThreadPoolExecutor(max_workers=workers)
        # Allow up to one waiting connection per worker before pausing accept.
        self._work_slots = BoundedSemaphore(workers * 2)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address) -> None:
        self._work_slots.acquire()
        try:
            self._workers.submit(self._handle_connection, request, client_address)
        except BaseException:
            self._work_slots.release()
            raise

    def _handle_connection(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._work_slots.release()

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self._workers.shutdown(wait=True)
