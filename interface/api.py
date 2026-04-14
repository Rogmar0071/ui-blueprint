from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from interface.cli import query_once


class EvidenceAPIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/query":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        query = str(payload.get("query", "")).strip()
        body = query_once(query)
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = HTTPServer((host, port), EvidenceAPIHandler)
    server.serve_forever()
