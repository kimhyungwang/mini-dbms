"""Run the mini DBMS as a TCP server with a fixed worker pool."""

from __future__ import annotations

import argparse
from socketserver import BaseRequestHandler, TCPServer

from mini_dbms import DuplicatePrimaryKeyError, MiniDBMS, SQLExecutionError, SelectResult
from server_transport import ProtocolError, WorkerPoolMixIn, receive_message, send_message
from sql_parser import SQLParseError, parse_sql


class QueryHandler(BaseRequestHandler):
    def handle(self) -> None:
        try:
            request = receive_message(self.request)
        except OSError:
            return
        except ProtocolError as error:
            self._reply({"ok": False, "error": str(error)})
            return

        sql = request.get("sql")
        if not isinstance(sql, str):
            self._reply({"ok": False, "error": "Expected a SQL string"})
            return

        try:
            result = self.server.database.execute(parse_sql(sql))
        except DuplicatePrimaryKeyError as error:
            self._reply({"ok": False, "code": "duplicate_primary_key", "error": str(error)})
            return
        except (SQLParseError, SQLExecutionError) as error:
            self._reply({"ok": False, "error": str(error)})
            return
        except OSError:
            self.server.handle_error(self.request, self.client_address)
            self._reply({"ok": False, "error": "Database storage error"})
            return

        if isinstance(result, SelectResult):
            self._reply({"ok": True, "columns": result.columns, "rows": result.rows})
        elif isinstance(result, int):
            self._reply({"ok": True, "inserted": result})
        else:
            self._reply({"ok": True, "status": result})

    def _reply(self, message: dict) -> None:
        try:
            try:
                send_message(self.request, message)
            except ProtocolError as error:
                send_message(self.request, {"ok": False, "error": str(error)})
        except OSError:
            # A client that timed out may have already closed its connection.
            pass


class DBServer(WorkerPoolMixIn, TCPServer):
    def __init__(self, address: tuple[str, int], database: MiniDBMS, workers: int) -> None:
        self.database = database
        super().__init__(address, QueryHandler, workers=workers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mini DBMS TCP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    with DBServer((args.host, args.port), MiniDBMS(args.data_dir), args.workers) as server:
        host, port = server.server_address
        print(f"DB listening on {host}:{port} with {args.workers} workers", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
