"""HTTP server (stdlib only): JSON API + static SPA hosting."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import guards
from .core import ApiError
from .db import connect, init_db

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "web")

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
    server_version = "EventReplay/1.0"

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
        if os.environ.get("REPLAY_LOG"):
            super().log_message(fmt, *args)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def _route(self, method: str):
        parts = urlsplit(self.path)
        try:
            if parts.path.startswith("/api/"):
                self._api(method, parts.path,
                          parse_qs(parts.query))
            else:
                self._static(parts.path)
        except ApiError as e:
            self._send_json(e.status, e.to_body())
        except Exception as e:  # noqa: BLE001 - last-resort guard
            self._send_json(500, {"error": "internal_error",
                                  "detail": str(e)})

    # -- API ---------------------------------------------------------------
    def _api(self, method: str, path: str, query: dict):
        body = self._read_json() if method in ("POST", "PUT") else {}
        conn = connect()
        try:
            init_db(conn)
            result = self._dispatch(conn, method, path, query, body)
            if result is None:
                result = {"ok": True}
            self._send_json(200, result)
        finally:
            conn.close()

    def _dispatch(self, conn, method: str, path: str, query: dict,
                  body: dict):
        p = path.strip("/").split("/")

        # /api/health
        if path == "/api/health":
            return {"ok": True, "service": "event-replay"}

        # /api/devices...
        if len(p) >= 2 and p[1] == "devices":
            if len(p) == 2:
                if method == "GET":
                    return {"devices": guards.list_devices(conn)}
                if method == "POST":
                    return guards.create_device(conn, body)
                raise ApiError(405, "method_not_allowed")
            device_id = p[2]
            if len(p) == 3 and method == "GET":
                return guards.get_device(conn, device_id)
            if len(p) == 4 and p[3] == "timeline" and method == "GET":
                return {"events": guards.get_raw_events(conn, device_id)}
            if len(p) == 5 and p[3] == "timeline" and p[4] == "build" \
                    and method == "POST":
                return guards.build_timeline(conn, device_id)
            if len(p) == 4 and p[3] == "versions" and method == "GET":
                return {"versions": guards.list_versions(conn, device_id)}
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

        # /api/batches...
        if len(p) >= 2 and p[1] == "batches":
            if len(p) == 2 and method == "POST":
                return guards.create_batch(conn, body)
            if len(p) >= 3:
                batch_id = p[2]
                if len(p) == 3 and method == "GET":
                    return guards.batch_status(conn, batch_id)
                if len(p) == 4 and p[3] == "commit" and method == "POST":
                    return guards.commit_batch(conn, batch_id)
                if len(p) == 5 and p[3] == "chunks" and method == "PUT":
                    return guards.put_chunk(conn, batch_id, int(p[4]), body)
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

        # /api/versions/{id}/timeline
        if len(p) == 4 and p[1] == "versions" and p[3] == "timeline" \
                and method == "GET":
            return guards.get_timeline(conn, p[2])

        # /api/findings...
        if len(p) >= 2 and p[1] == "findings":
            if len(p) == 2 and method == "GET":
                device_id = (query.get("device_id") or [None])[0]
                status = (query.get("status") or [None])[0]
                return {"findings": guards.list_findings(
                    conn, device_id=device_id, status=status)}
            if len(p) == 4 and p[3] == "review" and method == "POST":
                return guards.review_finding(conn, p[2], body)
            if len(p) == 4 and p[3] == "history" and method == "GET":
                return guards.finding_history(conn, p[2])
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

        # /api/reports...
        if len(p) == 3 and p[1] == "reports":
            if p[2] == "export" and method == "POST":
                return guards.export_report(conn, body)
            if p[2] == "verify" and method == "POST":
                return guards.verify_report(conn, body)
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

        # /api/seals...  一次性事件封存包
        if len(p) >= 2 and p[1] == "seals":
            if len(p) == 2 and method == "GET":
                device_id = (query.get("device_id") or [None])[0]
                return {"seals": guards.list_sealed_packages(
                    conn, device_id=device_id)}
            if len(p) == 3 and p[2] == "issue" and method == "POST":
                return guards.issue_sealed_package(conn, body)
            if len(p) == 4 and p[3] == "download" and method == "POST":
                return guards.download_sealed_package(conn, p[2], body)
            raise ApiError(404, "route_not_found", f"未知路径 {path}")

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


def make_server(host: str = "0.0.0.0", port: int = 8081,
                db_path: str | None = None) -> ThreadingHTTPServer:
    if db_path:
        from . import db as dbmod  # noqa: PLC0415
        dbmod.DB_PATH = db_path
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
