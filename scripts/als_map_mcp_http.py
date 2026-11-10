#!/usr/bin/env python3
"""
Streamable HTTP MCP server for the Total ALS Map.

Wraps the same tools / hard caps as als_map_mcp.py (stdio), over a single
MCP endpoint suitable for Cursor / Grok custom remote MCP once a public
hostname exists.

Transport: MCP Streamable HTTP (POST + optional GET SSE on /mcp).
Auth: Authorization: Bearer <ALS_MAP_MCP_TOKEN> required on every /mcp request.
Bind: 127.0.0.1 by default (set ALS_MAP_MCP_HOST / ALS_MAP_MCP_PORT).

Stdlib only — no FastAPI / mcp package.

Env:
  ALS_MAP_MCP_TOKEN   required bearer token (generate: openssl rand -hex 32)
  ALS_MAP_MCP_HOST    default 127.0.0.1
  ALS_MAP_MCP_PORT    default 8787
  ALS_MAP_DB          optional sqlite path
  ALS_MAP_MCP_ALLOWED_ORIGINS  comma list; empty = allow missing Origin + localhost

DNS note: mcp.austenstrine.dev is NOT assumed to exist yet — run locally /
behind a tunnel stub until Namecheap + Cloudflare are ready.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_mcp import (  # noqa: E402
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    handle_message,
    log,
)

HTTP_PROTOCOL_VERSION = "2025-03-26"  # Streamable HTTP era; tools still 2024-11-05-shaped
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MCP_PATH = "/mcp"


def require_token() -> str:
    token = os.environ.get("ALS_MAP_MCP_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "ALS_MAP_MCP_TOKEN is required.\n"
            "Generate one:  openssl rand -hex 32\n"
            "Then:          export ALS_MAP_MCP_TOKEN='…'\n"
            "Never commit the token."
        )
    return token


def allowed_origins() -> set[str] | None:
    raw = os.environ.get("ALS_MAP_MCP_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return None  # default localhost-friendly policy below
    return {o.strip() for o in raw.split(",") if o.strip()}


def origin_ok(origin: str | None) -> bool:
    """DNS-rebinding guard: reject present-but-disallowed Origin."""
    if not origin:
        return True
    allow = allowed_origins()
    if allow is not None:
        return origin in allow
    # Default: only loopback / empty-host style origins when no allowlist set
    try:
        parsed = urlparse(origin)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


class SessionStore:
    """Minimal session ids for Streamable HTTP clients that expect Mcp-Session-Id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        sid = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[sid] = time.time()
        return sid

    def touch(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            if sid not in self._sessions:
                return False
            self._sessions[sid] = time.time()
            return True

    def drop(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)


SESSIONS = SessionStore()


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sse_bytes(obj: Any, event_id: str = "1") -> bytes:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: message\ndata: {data}\n\n".encode("utf-8")


class MCPHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}-http/{SERVER_VERSION}"
    expected_token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.address_string()} - {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = _json_bytes({"error": "unauthorized", "hint": "Authorization: Bearer <ALS_MAP_MCP_TOKEN>"})
        self._send(401, body, "application/json", {"WWW-Authenticate": "Bearer"})

    def _forbidden_origin(self) -> None:
        body = _json_bytes({"jsonrpc": "2.0", "error": {"code": -32000, "message": "invalid Origin"}, "id": None})
        self._send(403, body, "application/json")

    def _check_auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._unauthorized()
            return False
        got = header[len("Bearer ") :].strip()
        if not got or not secrets.compare_digest(got, self.expected_token):
            self._unauthorized()
            return False
        return True

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin_ok(origin):
            return True
        self._forbidden_origin()
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            body = _json_bytes(
                {
                    "ok": True,
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "transport": "streamable-http",
                    "mcp_path": MCP_PATH,
                }
            )
            self._send(200, body, "application/json")
            return

        if path != MCP_PATH.rstrip("/") and path != "/mcp":
            self._send(404, _json_bytes({"error": "not found"}), "application/json")
            return

        if not self._check_origin() or not self._check_auth():
            return

        # Optional long-lived SSE listen; we advertise capability but keep a short idle stream.
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self._send(405, _json_bytes({"error": "GET requires Accept: text/event-stream"}), "application/json")
            return

        sid = self.headers.get("Mcp-Session-Id")
        extra = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
        if sid and SESSIONS.touch(sid):
            extra["Mcp-Session-Id"] = sid
        self.send_response(200)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        # Prime reconnect id, then close (clients may poll).
        try:
            self.wfile.write(b"id: 0\ndata: \n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != MCP_PATH.rstrip("/") and path != "/mcp":
            self._send(404, _json_bytes({"error": "not found"}), "application/json")
            return

        if not self._check_origin() or not self._check_auth():
            return

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            msg = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError:
            self._send(400, _json_bytes({"error": "invalid JSON"}), "application/json")
            return

        if not isinstance(msg, dict):
            self._send(400, _json_bytes({"error": "JSON-RPC message must be an object"}), "application/json")
            return

        # Notifications / responses: 202 Accepted, no body
        is_notification = "id" not in msg and msg.get("method")
        is_response = "id" in msg and "method" not in msg and ("result" in msg or "error" in msg)
        if is_notification or is_response:
            try:
                if is_notification:
                    handle_message(msg)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
                self._send(400, b"", "application/json")
                return
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Requests
        method = msg.get("method")
        session_header = self.headers.get("Mcp-Session-Id")
        extra: dict[str, str] = {}

        if method == "notifications/initialized":
            # Should arrive as notification (no id); defensive
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            resp = handle_message(msg)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            resp = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }

        if method == "initialize" and resp and "result" in resp:
            # Prefer Streamable HTTP protocol version in the initialize result.
            resp["result"]["protocolVersion"] = HTTP_PROTOCOL_VERSION
            resp["result"]["serverInfo"] = {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "title": "ALS Map (HTTP)",
            }
            sid = SESSIONS.create()
            extra["Mcp-Session-Id"] = sid
        elif session_header:
            if SESSIONS.touch(session_header):
                extra["Mcp-Session-Id"] = session_header
            # Unknown / missing session: still serve (stateless OK).

        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            if session_header:
                self.send_header("Mcp-Session-Id", session_header)
            self.end_headers()
            return

        accept = self.headers.get("Accept", "")
        prefer_sse = "text/event-stream" in accept and "application/json" not in accept
        # Default to JSON (simpler for curl / most custom MCP paste configs).
        if prefer_sse:
            body = _sse_bytes(resp)
            # Include priming empty event? Spec wants eventual response; one event is enough.
            self._send(200, body, "text/event-stream", extra)
        else:
            self._send(200, _json_bytes(resp), "application/json", extra)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in {MCP_PATH.rstrip("/"), "/mcp"}:
            self._send(404, _json_bytes({"error": "not found"}), "application/json")
            return
        if not self._check_origin() or not self._check_auth():
            return
        SESSIONS.drop(self.headers.get("Mcp-Session-Id"))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Minimal CORS preflight for browser-based clients; auth still required on real calls.
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin and origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> int:
    token = require_token()
    host = os.environ.get("ALS_MAP_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.environ.get("ALS_MAP_MCP_PORT", str(DEFAULT_PORT)))

    MCPHandler.expected_token = token
    httpd = ThreadingHTTPServer((host, port), MCPHandler)

    log(
        f"{SERVER_NAME} HTTP {SERVER_VERSION} on http://{host}:{port}{MCP_PATH} "
        f"(protocol {HTTP_PROTOCOL_VERSION}; health /health)"
    )
    log("Auth: Bearer token from ALS_MAP_MCP_TOKEN (required)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
