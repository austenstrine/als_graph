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
  ALS_MAP_MCP_ALLOW_NONLOCAL   set to 1 to bind non-loopback (default refuse)
  ALS_MAP_ACCESS_LOG  access log path (default ~/.local/state/als_map/access.log)

DNS note: mcp.austenstrine.dev is NOT assumed to exist yet — run locally /
behind a tunnel stub until Namecheap + Cloudflare are ready.
"""
from __future__ import annotations

import ipaddress
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
MAX_BODY_BYTES = 1_048_576  # 1 MiB JSON-RPC body cap
SESSION_TTL_SEC = 3600
DEFAULT_ACCESS_LOG = Path.home() / ".local" / "state" / "als_map" / "access.log"
_ACCESS_LOG_LOCK = threading.Lock()


def access_log_path() -> Path:
    raw = os.environ.get("ALS_MAP_ACCESS_LOG", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_ACCESS_LOG


def write_access_log(
    *,
    client_ip: str,
    method: str,
    path: str,
    status: int,
    mcp_method: str | None = None,
    tool: str | None = None,
) -> None:
    """Append one access line. Never logs Authorization / bearer values."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fields = [
        ts,
        client_ip or "-",
        method or "-",
        path or "-",
        str(status),
        mcp_method or "-",
        tool or "-",
    ]
    # Space-separated; no header values — keep parseable and secret-safe.
    line = " ".join(fields) + "\n"
    path_out = access_log_path()
    try:
        path_out.parent.mkdir(parents=True, exist_ok=True)
        with _ACCESS_LOG_LOCK:
            with path_out.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        log(f"access log write failed: {exc}")


def parse_mcp_meta(msg: Any) -> tuple[str | None, str | None]:
    """Return (jsonrpc_method, tool_name) when parseable from a JSON-RPC object."""
    if not isinstance(msg, dict):
        return None, None
    mcp_method = msg.get("method") if isinstance(msg.get("method"), str) else None
    tool = None
    if mcp_method == "tools/call":
        params = msg.get("params")
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str) and name:
                tool = name
    return mcp_method, tool


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

    def __init__(self, ttl_sec: float = SESSION_TTL_SEC) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._ttl = ttl_sec

    def _prune_unlocked(self, now: float) -> None:
        expired = [sid for sid, ts in self._sessions.items() if now - ts > self._ttl]
        for sid in expired:
            self._sessions.pop(sid, None)

    def create(self) -> str:
        sid = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._prune_unlocked(now)
            self._sessions[sid] = now
        return sid

    def touch(self, sid: str | None) -> bool:
        if not sid:
            return False
        now = time.time()
        with self._lock:
            self._prune_unlocked(now)
            if sid not in self._sessions:
                return False
            self._sessions[sid] = now
            return True

    def drop(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)


def is_loopback_host(host: str) -> bool:
    """True for localhost names and loopback IPs (incl. ::1 / 127.0.0.0/8)."""
    h = host.strip().lower().strip("[]")
    if h in {"localhost", "::1"} or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def assert_bind_host(host: str) -> None:
    allow = os.environ.get("ALS_MAP_MCP_ALLOW_NONLOCAL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if is_loopback_host(host):
        return
    if allow:
        log(f"WARNING: binding non-loopback host {host!r} (ALS_MAP_MCP_ALLOW_NONLOCAL set)")
        return
    raise SystemExit(
        f"Refusing to bind non-loopback host {host!r}.\n"
        "Default is loopback-only. For a tunnel, keep ALS_MAP_MCP_HOST=127.0.0.1\n"
        "and point cloudflared at that. To override intentionally:\n"
        "  export ALS_MAP_MCP_ALLOW_NONLOCAL=1"
    )


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
        # Never echo Authorization / Bearer material if a custom format includes it.
        try:
            rendered = fmt % args
        except Exception:  # noqa: BLE001
            rendered = fmt
        lowered = rendered.lower()
        if "authorization" in lowered or "bearer " in lowered:
            rendered = "<redacted request log>"
        log(f"{self.address_string()} - {rendered}")

    def _client_ip(self) -> str:
        for header in ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP"):
            value = (self.headers.get(header) or "").strip()
            if value:
                return value
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip() or self.client_address[0]
        return self.client_address[0]

    def _request_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def _access(
        self,
        status: int,
        path: str | None = None,
        mcp_method: str | None = None,
        tool: str | None = None,
    ) -> None:
        write_access_log(
            client_ip=self._client_ip(),
            method=self.command or "-",
            path=path or self._request_path(),
            status=status,
            mcp_method=mcp_method,
            tool=tool,
        )

    def _read_body(self) -> bytes | None:
        raw_len = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_len)
        except ValueError:
            self._send(400, _json_bytes({"error": "invalid Content-Length"}), "application/json")
            return None
        if length < 0:
            self._send(400, _json_bytes({"error": "invalid Content-Length"}), "application/json")
            return None
        if length > MAX_BODY_BYTES:
            self._send(
                413,
                _json_bytes({"error": "payload too large", "max_bytes": MAX_BODY_BYTES}),
                "application/json",
            )
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
        *,
        path: str | None = None,
        mcp_method: str | None = None,
        tool: str | None = None,
        access: bool = True,
    ) -> None:
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
        if access:
            self._access(code, path=path, mcp_method=mcp_method, tool=tool)

    def _send_empty(self, code: int, extra: dict[str, str] | None = None, **access_kw: Any) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self._access(code, **access_kw)

    def _unauthorized(self, path: str | None = None) -> None:
        body = _json_bytes({"error": "unauthorized", "hint": "Authorization: Bearer <ALS_MAP_MCP_TOKEN>"})
        self._send(401, body, "application/json", {"WWW-Authenticate": "Bearer"}, path=path)

    def _forbidden_origin(self, path: str | None = None) -> None:
        body = _json_bytes({"jsonrpc": "2.0", "error": {"code": -32000, "message": "invalid Origin"}, "id": None})
        self._send(403, body, "application/json", path=path)

    def _check_auth(self, path: str | None = None) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._unauthorized(path=path)
            return False
        got = header[len("Bearer ") :].strip()
        if not got or not secrets.compare_digest(got, self.expected_token):
            self._unauthorized(path=path)
            return False
        return True

    def _check_origin(self, path: str | None = None) -> bool:
        origin = self.headers.get("Origin")
        if origin_ok(origin):
            return True
        self._forbidden_origin(path=path)
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = self._request_path()
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
            self._send(200, body, "application/json", path=path)
            return

        if path != MCP_PATH.rstrip("/") and path != "/mcp":
            self._send(404, _json_bytes({"error": "not found"}), "application/json", path=path)
            return

        if not self._check_origin(path=path) or not self._check_auth(path=path):
            return

        # Optional long-lived SSE listen; we advertise capability but keep a short idle stream.
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self._send(
                405,
                _json_bytes({"error": "GET requires Accept: text/event-stream"}),
                "application/json",
                path=path,
            )
            return

        sid = self.headers.get("Mcp-Session-Id")
        extra = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
        if sid and SESSIONS.touch(sid):
            extra["Mcp-Session-Id"] = sid
        self.send_response(200)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self._access(200, path=path, mcp_method="sse")
        # Prime reconnect id, then close (clients may poll).
        try:
            self.wfile.write(b"id: 0\ndata: \n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        path = self._request_path()
        if path != MCP_PATH.rstrip("/") and path != "/mcp":
            self._send(404, _json_bytes({"error": "not found"}), "application/json", path=path)
            return

        if not self._check_origin(path=path) or not self._check_auth(path=path):
            return

        raw = self._read_body()
        if raw is None:
            return
        try:
            msg = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError:
            self._send(400, _json_bytes({"error": "invalid JSON"}), "application/json", path=path)
            return

        if not isinstance(msg, dict):
            self._send(
                400,
                _json_bytes({"error": "JSON-RPC message must be an object"}),
                "application/json",
                path=path,
            )
            return

        mcp_method, tool = parse_mcp_meta(msg)

        # Notifications / responses: 202 Accepted, no body
        is_notification = "id" not in msg and msg.get("method")
        is_response = "id" in msg and "method" not in msg and ("result" in msg or "error" in msg)
        if is_notification or is_response:
            try:
                if is_notification:
                    handle_message(msg)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
                self._send(400, b"", "application/json", path=path, mcp_method=mcp_method, tool=tool)
                return
            self._send_empty(202, path=path, mcp_method=mcp_method, tool=tool)
            return

        # Requests
        method = msg.get("method")
        session_header = self.headers.get("Mcp-Session-Id")
        extra: dict[str, str] = {}

        if method == "notifications/initialized":
            # Should arrive as notification (no id); defensive
            self._send_empty(202, path=path, mcp_method=mcp_method, tool=tool)
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
            extra_out = {"Mcp-Session-Id": session_header} if session_header else None
            self._send_empty(202, extra=extra_out, path=path, mcp_method=mcp_method, tool=tool)
            return

        accept = self.headers.get("Accept", "")
        prefer_sse = "text/event-stream" in accept and "application/json" not in accept
        # Default to JSON (simpler for curl / most custom MCP paste configs).
        if prefer_sse:
            body = _sse_bytes(resp)
            # Include priming empty event? Spec wants eventual response; one event is enough.
            self._send(
                200,
                body,
                "text/event-stream",
                extra,
                path=path,
                mcp_method=mcp_method,
                tool=tool,
            )
        else:
            self._send(
                200,
                _json_bytes(resp),
                "application/json",
                extra,
                path=path,
                mcp_method=mcp_method,
                tool=tool,
            )

    def do_DELETE(self) -> None:  # noqa: N802
        path = self._request_path()
        if path not in {MCP_PATH.rstrip("/"), "/mcp"}:
            self._send(404, _json_bytes({"error": "not found"}), "application/json", path=path)
            return
        if not self._check_origin(path=path) or not self._check_auth(path=path):
            return
        SESSIONS.drop(self.headers.get("Mcp-Session-Id"))
        self._send_empty(204, path=path)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Minimal CORS preflight for browser-based clients; auth still required on real calls.
        path = self._request_path()
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
        self._access(204, path=path)


def main() -> int:
    token = require_token()
    host = os.environ.get("ALS_MAP_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.environ.get("ALS_MAP_MCP_PORT", str(DEFAULT_PORT)))
    assert_bind_host(host)

    MCPHandler.expected_token = token
    httpd = ThreadingHTTPServer((host, port), MCPHandler)

    log(
        f"{SERVER_NAME} HTTP {SERVER_VERSION} on http://{host}:{port}{MCP_PATH} "
        f"(protocol {HTTP_PROTOCOL_VERSION}; health /health; max_body {MAX_BODY_BYTES})"
    )
    log("Auth: Bearer token from ALS_MAP_MCP_TOKEN (required; value never logged)")
    log(f"Access log: {access_log_path()} (ts ip method path status mcp_method tool)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
