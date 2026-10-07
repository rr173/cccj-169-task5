"""HTTP server (stdlib only): JSON API + static SPA hosting."""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import guards
from .core import ApiError
from .db import DB_PATH, connect, init_db

WEB_ROOT = "/workspace/platform/web"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _json_default(obj):
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "PrintTrace/1.0"

    # -- response helpers --------------------------------------------------
    def _send_json(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return body

    def log_message(self, fmt, *args):  # quieter logging
        if os.environ.get("PRINT_TRACE_LOG"):
            super().log_message(fmt, *args)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        parts = urlsplit(self.path)
        path = parts.path
        try:
            if path.startswith("/api/"):
                self._api(method, path)
            else:
                self._static(path)
        except ApiError as e:
            self._send_json(e.status, e.to_body())
        except Exception as e:  # noqa: BLE001 - last-resort guard
            self._send_json(500, {"error": "internal_error",
                                  "detail": str(e)})

    # -- API ---------------------------------------------------------------
    def _api(self, method: str, path: str):
        body = self._read_json() if method == "POST" else {}
        conn = connect()
        try:
            init_db(conn)
            result = self._dispatch(conn, method, path, body)
            if result is None:
                result = {"ok": True}
            self._send_json(200, result)
        finally:
            conn.close()

    def _dispatch(self, conn, method: str, path: str, body: dict):
        p = path.strip("/").split("/")
        # /api/jobs...
        if len(p) >= 2 and p[1] == "jobs":
            if len(p) == 2:
                if method == "GET":
                    return {"jobs": guards.list_jobs(conn)}
                if method == "POST":
                    return guards.create_job(conn, body)
                raise ApiError(405, "method_not_allowed")
            job_id = p[2]
            if len(p) == 3:
                if method == "GET":
                    return guards.get_job(conn, job_id)
                raise ApiError(405, "method_not_allowed")
            sub = p[3]
            if sub == "revise" and method == "POST" and len(p) == 4:
                return guards.revise_job(conn, job_id, body)
            if sub == "materials" and method == "POST" and len(p) == 4:
                return guards.declare_material(conn, job_id, body)
            if sub == "ledger" and len(p) == 5:
                if p[4] == "charge" and method == "POST":
                    return guards.charge_material(conn, job_id, body)
                if p[4] == "return" and method == "POST":
                    return guards.return_material(conn, job_id, body)
            if sub == "material" and p[4] == "close" and len(p) == 5 \
                    and method == "POST":
                return guards.close_material(conn, job_id)
            if sub == "sessions":
                if len(p) == 4 and method == "POST":
                    return guards.create_session(conn, job_id, body)
                # /jobs/{id}/sessions/{sid}/end
                if len(p) == 6 and p[5] == "end" and method == "POST":
                    return guards.end_session(conn, job_id, p[4], body)
                raise ApiError(404, "route_not_found")
            if sub == "layers" and len(p) == 4 and method == "POST":
                return guards.upload_layer(conn, job_id, body)
            if sub == "anomalies" and len(p) == 6 and p[5] == "resolve" \
                    and method == "POST":
                return guards.resolve_anomaly(conn, job_id, int(p[4]), body)
            if sub == "inspections" and len(p) == 4 and method == "POST":
                return guards.add_inspection(conn, job_id, body)
            if sub == "gate" and len(p) == 4 and method == "GET":
                return guards.evaluate_gate(conn, job_id)
            if sub == "release" and len(p) == 4 and method == "POST":
                return guards.release_product(conn, job_id, body)
            if sub == "scrap" and len(p) == 4 and method == "POST":
                return guards.scrap_product(conn, job_id, body)
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

        # /api/certificates...
        if len(p) >= 2 and p[1] == "certificates":
            if method == "POST" and len(p) == 3 and p[2] == "verify":
                return guards.verify_certificate(conn, body)
            if method == "GET" and len(p) == 3:
                return guards.get_certificate(conn, cert_id=p[2])
            raise ApiError(404, "route_not_found")

        # /api/products/{serial}/certificate
        if len(p) == 4 and p[1] == "products" and p[3] == "certificate" \
                and method == "GET":
            return guards.get_certificate(conn, serial=p[2])

        # /api/health
        if path == "/api/health":
            return {"ok": True, "service": "print-trace"}

        raise ApiError(404, "route_not_found", f"未知路径 {path}")

    # -- static ------------------------------------------------------------
    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        # prevent path traversal
        rel = os.path.normpath(path).lstrip("/")
        full = os.path.join(WEB_ROOT, rel)
        if not os.path.abspath(full).startswith(
                os.path.abspath(WEB_ROOT) + os.sep) \
                and os.path.abspath(full) != os.path.abspath(
                    os.path.join(WEB_ROOT, "index.html")):
            self._send_json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(full):
            # SPA fallback
            full = os.path.join(WEB_ROOT, "index.html")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json(404, {"error": "not_found"})
            return
        ext = os.path.splitext(full)[1]
        self.send_response(200)
        self.send_header("Content-Type",
                         _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
