"""Serve user lookup and creation over HTTP using the mini DBMS server."""

from __future__ import annotations

import argparse
import json
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import isfinite

from server_transport import ProtocolError, WorkerPoolMixIn, receive_message, send_message

MAX_USER_BODY_BYTES = 64 * 1024


class UserHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = self.path.partition("?")[0].split("/")
        if len(parts) != 3 or parts[:2] != ["", "users"]:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
            return

        try:
            if re.fullmatch(r"-?[0-9]+", parts[2]) is None:
                raise ValueError("Invalid user ID")
            user_id = int(parts[2])
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "User ID must be an integer"})
            return

        sql = f"SELECT id, name FROM users WHERE id = {user_id};"
        response = self._query_db(sql)
        if response is not None:
            status, body = self._user_result(response)
            self._send_json(status, body)

    def do_POST(self) -> None:
        if self.path.partition("?")[0] != "/users":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
            return

        user = self._read_new_user()
        if user is None:
            return

        user_id, name = user
        # The SQL parser reads a doubled quote as one quote in a string value.
        escaped_name = name.replace("'", "''")
        sql = f"INSERT INTO users (id, name) VALUES ({user_id}, '{escaped_name}');"
        response = self._query_db(sql)
        if response is None:
            return
        if response.get("ok") is True and response.get("inserted") == 1:
            self._send_json(
                HTTPStatus.CREATED, {"id": user_id, "name": name}, location=f"/users/{user_id}"
            )
        elif response.get("code") == "duplicate_primary_key":
            self._send_json(HTTPStatus.CONFLICT, {"error": "User ID already exists"})
        else:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Database insert failed"})

    def _read_new_user(self) -> tuple[int, str] | None:
        if self.headers.get_content_type() != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Expected application/json"})
            return None
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Use Content-Length for the request body"})
            return None

        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return None
        length = lengths[0].strip()
        try:
            if len(lengths) != 1 or re.fullmatch(r"[0-9]+", length) is None:
                raise ValueError("Invalid Content-Length")
            size = int(length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None
        if size > MAX_USER_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body is too large"})
            return None

        try:
            body = self.rfile.read(size)
        except (TimeoutError, socket.timeout):
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "Request body timed out"})
            return None
        except OSError:
            self.close_connection = True
            return None

        try:
            if len(body) != size:
                raise ValueError("Incomplete request body")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            user_id, name = payload.get("id"), payload.get("name")
            if type(user_id) is not int or not isinstance(name, str) or not name.strip():
                raise ValueError("Expected an integer id and a non-empty name")
            name.encode("utf-8")
        except (ValueError, RecursionError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected an integer id and a non-empty name"})
            return None
        return user_id, name

    def _query_db(self, sql: str) -> dict | None:
        try:
            with socket.create_connection(self.server.db_address, self.server.db_timeout) as connection:
                send_message(connection, {"sql": sql})
                return receive_message(connection)
        except (TimeoutError, socket.timeout):
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Database response timed out"})
        except (OSError, ProtocolError):
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Database connection failed"})
        return None

    @staticmethod
    def _user_result(response: dict) -> tuple[HTTPStatus, dict]:
        rows = response.get("rows")
        if response.get("ok") is not True or response.get("columns") != ["id", "name"]:
            return HTTPStatus.BAD_GATEWAY, {"error": "Database query failed"}
        if not isinstance(rows, list):
            return HTTPStatus.BAD_GATEWAY, {"error": "Invalid database response"}
        if not rows:
            return HTTPStatus.NOT_FOUND, {"error": "User not found"}
        if len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != 2:
            return HTTPStatus.BAD_GATEWAY, {"error": "Expected one user record"}

        found_id, name = rows[0]
        return HTTPStatus.OK, {"id": found_id, "name": name}

    def _send_json(self, status: HTTPStatus, message: dict, *, location: str | None = None) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


class APIServer(WorkerPoolMixIn, HTTPServer):
    def __init__(
        self, address: tuple[str, int], db_address: tuple[str, int], db_timeout: float, workers: int
    ) -> None:
        self.db_address = db_address
        self.db_timeout = db_timeout
        super().__init__(address, UserHandler, workers=workers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the HTTP API for the mini DBMS.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=9000)
    parser.add_argument("--db-timeout", type=float, default=5)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if not isfinite(args.db_timeout) or args.db_timeout <= 0:
        parser.error("--db-timeout must be a positive number of seconds")

    with APIServer(
        (args.host, args.port), (args.db_host, args.db_port), args.db_timeout, args.workers
    ) as server:
        host, port = server.server_address
        print(f"API listening on http://{host}:{port} with {args.workers} workers", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
