"""Ask a running T3 Code to rescan its usage before the card reads T3's scan cache.

T3 Code rescans its usage sources (Codex, Grok and the other tools it reads) and saves its scan cache only
when something calls its `server.getUsageSummary` method, which its usage page does. Without this step the
card's Codex and Grok numbers are as old as the last visit to that page.

The call, as T3 Code 0.1.12 serves it (checked against its bundled server in November 2026):

1. `t3 auth session issue --scope diagnostics:read --ttl 5m --json`, T3's own command for headless clients,
   gives a bearer token that can only read diagnostics (the usage summary needs `diagnostics:read`).
2. A WebSocket to `ws://127.0.0.1:<port>/ws?orchestrationProtocol=2`, the port from T3's
   `userdata/server-runtime.json`, with the token in the `Authorization` header.
3. One Effect RPC request in JSON: `{"_tag": "Request", "id": "1", "tag": "server.getUsageSummary",
   "payload": {sinceDay, untilDay, timeZone, awaitRefresh: true}, "headers": []}`; T3 answers with an
   `Exit` message once the scan is done and then saves its cache.
4. `t3 auth session revoke <id>`, so the session is gone at once rather than in five minutes.

The token lives only in this process: it is never written to disk or to the log. Every failure (T3 Code not
running, an older or newer T3 that answers differently, a timeout) is logged as one warning and the card
reads the cache as it is, which is what it did before this step existed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("ai-usage-card")

RUNTIME_FILE = "server-runtime.json"
ORCHESTRATION_PROTOCOL = 2
METHOD = "server.getUsageSummary"
SCOPE = "diagnostics:read"
SESSION_TTL = "5m"
SESSION_LABEL = "ai-usage-card refresh"
CLI_TIMEOUT_S = 60.0
SCAN_TIMEOUT_S = 180.0
SCAN_DAYS = 31  # the card's live days (FREEZE_AFTER_DAYS) and one more; older days come from the saved history
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RefreshError(Exception):
    """T3 Code did not rescan. The message never carries the token."""


def default_cli(userdata: Path) -> Path:
    """The `t3` command T3 Code installs in its home folder (the parent of `userdata`)."""
    return userdata.parent / "bin" / "t3"


def refresh_t3_usage(userdata: Path, today: date, timezone: str, cli: Optional[Path] = None,
                     run: Runner = subprocess.run, timeout_s: float = SCAN_TIMEOUT_S) -> bool:
    """Asks T3 Code to rescan the last `SCAN_DAYS` days. True when it answered; False, with a warning in the
    log, when it could not be asked or did not answer. Never raises."""
    started = time.monotonic()
    try:
        port = _server_port(userdata)
        command = cli or default_cli(userdata)
        if not os.access(str(command), os.X_OK):
            raise RefreshError("T3 Code's command is not installed")
        env = dict(os.environ, T3CODE_HOME=str(userdata.parent))
        session_id, token = _issue_session(command, run, env)
        try:
            payload = {"sinceDay": (today - timedelta(days=SCAN_DAYS - 1)).isoformat(), "untilDay": today.isoformat(),
                       "timeZone": timezone, "awaitRefresh": True}
            summary = _call(port, token, payload, timeout_s)
        finally:
            _revoke_session(command, run, env, session_id)
        log.info("T3 Code rescanned its usage in %.1fs (%d sources)", time.monotonic() - started,
                 len(summary.get("sources") or []))
        return True
    except RefreshError as error:
        reason = str(error)
    except Exception as error:  # a refresh must never stop the card
        reason = "%s" % type(error).__name__
    log.warning("T3 Code was not asked to rescan its usage (%s); reading its cache as it is", reason)
    return False


def _server_port(userdata: Path) -> int:
    try:
        runtime = json.loads((userdata / RUNTIME_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RefreshError("T3 Code is not running")
    except (OSError, ValueError) as error:
        raise RefreshError("T3 Code's runtime file cannot be read (%s)" % type(error).__name__)
    port = runtime.get("port") if isinstance(runtime, dict) else None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise RefreshError("T3 Code's runtime file has no port")
    return port


def _issue_session(command: Path, run: Runner, env: Dict[str, str]):
    try:
        done = run([str(command), "auth", "session", "issue", "--scope", SCOPE, "--ttl", SESSION_TTL,
                    "--label", SESSION_LABEL, "--json"], capture_output=True, text=True, timeout=CLI_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        raise RefreshError("T3 Code's command did not issue a session in %.0fs" % CLI_TIMEOUT_S)
    if done.returncode != 0:  # its output is not logged: it is T3's, not ours, and could change
        raise RefreshError("T3 Code's command refused to issue a session (exit %d)" % done.returncode)
    try:
        issued = json.loads(done.stdout)
        session_id, token = issued["sessionId"], issued["token"]
    except (ValueError, TypeError, KeyError):
        raise RefreshError("T3 Code's command answered in a form this card does not know")
    if not (isinstance(session_id, str) and session_id and isinstance(token, str) and token):
        raise RefreshError("T3 Code's command answered in a form this card does not know")
    return session_id, token


def _revoke_session(command: Path, run: Runner, env: Dict[str, str], session_id: str) -> None:
    try:
        done = run([str(command), "auth", "session", "revoke", session_id], capture_output=True, text=True,
                   timeout=CLI_TIMEOUT_S, env=env)
        failed = done.returncode != 0
    except Exception:
        failed = True
    if failed:
        log.warning("T3 Code did not revoke the card's session; it expires by itself within %s", SESSION_TTL)


def _call(port: int, token: str, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=min(10.0, timeout_s))
    except OSError:
        raise RefreshError("T3 Code is not answering on its port")
    with sock:
        _handshake(sock, port, token, deadline)
        request = {"_tag": "Request", "id": "1", "tag": METHOD, "payload": payload, "headers": []}
        _send_frame(sock, 0x1, json.dumps(request).encode("utf-8"))
        while True:
            try:
                messages = json.loads(_read_message(sock, deadline).decode("utf-8"))
            except ValueError:
                raise RefreshError("T3 Code answered with something other than JSON")
            for message in messages if isinstance(messages, list) else [messages]:
                if isinstance(message, dict) and message.get("_tag") in ("Defect", "ClientProtocolError"):
                    raise RefreshError("T3 Code reported a protocol error")
                if not isinstance(message, dict) or message.get("_tag") != "Exit" or message.get("requestId") != "1":
                    continue  # pings, acknowledgements and anything else T3 adds later
                exit_ = message.get("exit") or {}
                if exit_.get("_tag") == "Success" and isinstance(exit_.get("value"), dict):
                    return exit_["value"]
                raise RefreshError("T3 Code refused the usage request")


def _handshake(sock: socket.socket, port: int, token: str, deadline: float) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall(("GET /ws?orchestrationProtocol=%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
                  "Authorization: Bearer %s\r\n\r\n" % (ORCHESTRATION_PROTOCOL, port, key, token)).encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        if len(head) > 65536:
            raise RefreshError("T3 Code sent an oversized reply")
        head += _recv(sock, 1, deadline)
    lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    status = lines[0].split(" ")
    if len(status) < 2 or status[1] != "101":
        raise RefreshError("T3 Code refused the connection (HTTP %s)" % (status[1] if len(status) > 1 else "?"))
    headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
    if headers.get("sec-websocket-accept") != accept_for(key):
        raise RefreshError("T3 Code's WebSocket answer did not match")


def accept_for(key: str) -> str:
    """The `Sec-WebSocket-Accept` a server must answer `key` with (RFC 6455, section 4.2.2)."""
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")


def _recv(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        left = deadline - time.monotonic()
        if left <= 0:
            raise RefreshError("T3 Code did not answer in time")
        sock.settimeout(left)
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout:
            raise RefreshError("T3 Code did not answer in time")
        if not chunk:
            raise RefreshError("T3 Code closed the connection")
        data += chunk
    return bytes(data)


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """One final frame from a client, masked as RFC 6455 requires."""
    size = len(payload)
    if size < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
    elif size < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
    mask = os.urandom(4)
    sock.sendall(head + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))


def _read_message(sock: socket.socket, deadline: float) -> bytes:
    """The next whole data message, answering pings on the way."""
    data = bytearray()
    while True:
        first, second = _recv(sock, 2, deadline)
        opcode, size = first & 0x0F, second & 0x7F
        if size == 126:
            size = struct.unpack("!H", _recv(sock, 2, deadline))[0]
        elif size == 127:
            size = struct.unpack("!Q", _recv(sock, 8, deadline))[0]
        mask = _recv(sock, 4, deadline) if second & 0x80 else b""
        payload = _recv(sock, size, deadline) if size else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        if opcode == 0x8:
            raise RefreshError("T3 Code closed the connection")
        if opcode == 0x9:
            _send_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        data += payload
        if first & 0x80:
            return bytes(data)
