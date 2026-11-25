"""The step that asks T3 Code to rescan its usage: a fake T3 server and a fake `t3` command, never the real ones."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ai_usage_card import cli, refresh
from tests import fixtures as fx

TODAY = date(2026, 10, 18)
TOKEN = "fake-token-0123456789"
SESSION = "fake-session-id"
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeT3(threading.Thread):
    """A one-connection stand-in for T3's /ws endpoint. `mode` picks its answer."""

    def __init__(self, mode: str = "success"):
        super().__init__(daemon=True)
        self.mode = mode
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.request_line = ""
        self.authorization = ""
        self.request = None
        self.stop = threading.Event()

    def run(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(1)
                if not chunk:
                    return
                head += chunk
            lines = head.decode("latin-1").split("\r\n")
            self.request_line = lines[0]
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
            self.authorization = headers.get("authorization", "")
            if self.mode == "unauthorised":
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(hashlib.sha1(headers["sec-websocket-key"].encode() + GUID).digest()).decode()
            if self.mode == "wrong-accept":
                accept = base64.b64encode(b"not the right answer").decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            self.request = json.loads(self._read_frame(conn))
            if self.mode == "silent":
                self.stop.wait(5)
                return
            if self.mode == "failure":
                reply = {"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Failure", "cause": []}}
            else:
                summary = {"contractVersion": 6, "sources": [{"fingerprint": {"provider": "codex"}, "status": "ok"}] * 3,
                           "buckets": [{"n": i} for i in range(3000)]}  # large enough for a 64-bit length
                reply = [{"_tag": "Chunk", "requestId": "9", "values": []},
                         {"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Success", "value": summary}}]
            conn.sendall(self._frame(0x9, b"ping"))  # a ping first, which the client answers and skips
            body = json.dumps(reply).encode()
            middle = len(body) // 2  # and the reply in two fragments
            conn.sendall(self._frame(0x1, body[:middle], fin=False) + self._frame(0x0, body[middle:]))
            self.stop.wait(1)

    @staticmethod
    def _frame(opcode, payload, fin=True):
        size = len(payload)
        first = (0x80 if fin else 0) | opcode
        if size < 126:
            return struct.pack("!BB", first, size) + payload
        if size < 65536:
            return struct.pack("!BBH", first, 126, size) + payload
        return struct.pack("!BBQ", first, 127, size) + payload

    @staticmethod
    def _read_frame(conn):
        def exact(n):
            data = b""
            while len(data) < n:
                data += conn.recv(n - len(data))
            return data
        first, second = exact(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", exact(8))[0]
        mask = exact(4)
        return bytes(b ^ mask[i % 4] for i, b in enumerate(exact(size)))

    def close(self):
        self.stop.set()
        self.server.close()


class FakeCli:
    """Records each `t3` call and answers the way T3's command does."""

    def __init__(self, issue_exit=0, issue_stdout=None, revoke_exit=0):
        self.calls = []
        self.issue_exit, self.revoke_exit = issue_exit, revoke_exit
        self.issue_stdout = issue_stdout if issue_stdout is not None else json.dumps(
            {"sessionId": SESSION, "token": TOKEN, "scopes": ["diagnostics:read"]})

    def __call__(self, args, **kwargs):
        self.calls.append((args[1:4], kwargs))
        if args[1:4] == ["auth", "session", "issue"]:
            return subprocess.CompletedProcess(args, self.issue_exit, self.issue_stdout, "")
        return subprocess.CompletedProcess(args, self.revoke_exit, "", "")

    @property
    def verbs(self):
        return [call[0][2] for call in self.calls]


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "t3home"
        self.userdata = self.home / "userdata"
        self.userdata.mkdir(parents=True)
        self.cli_path = self.home / "bin" / "t3"
        self.cli_path.parent.mkdir()
        self.cli_path.write_text("#!/bin/sh\nexit 99\n")  # never run: every test passes a fake runner
        self.cli_path.chmod(0o755)

    def start(self, mode="success"):
        server = FakeT3(mode)
        server.start()
        self.addCleanup(server.close)
        (self.userdata / "server-runtime.json").write_text(json.dumps({"version": 1, "port": server.port}))
        return server

    def refresh(self, run, timeout_s=10.0):
        with self.assertLogs("ai-usage-card", level="INFO") as logs:
            result = refresh.refresh_t3_usage(self.userdata, TODAY, "Asia/Dhaka", run=run, timeout_s=timeout_s)
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        return result, text

    def test_a_running_t3_is_asked_to_rescan_and_the_session_is_revoked(self):
        server, run = self.start(), FakeCli()
        result, text = self.refresh(run)
        self.assertTrue(result)
        self.assertIn("T3 Code rescanned its usage", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])
        issue_args, issue_kwargs = run.calls[0]
        self.assertEqual(issue_kwargs["env"]["T3CODE_HOME"], str(self.home))
        self.assertEqual(server.request_line, "GET /ws?orchestrationProtocol=2 HTTP/1.1")
        self.assertEqual(server.authorization, "Bearer " + TOKEN)
        self.assertEqual(server.request["tag"], "server.getUsageSummary")
        self.assertEqual(server.request["payload"], {"sinceDay": "2026-09-18", "untilDay": "2026-10-18",
                                                     "timeZone": "Asia/Dhaka", "awaitRefresh": True})

    def test_t3_not_running_falls_back_without_calling_its_command(self):
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("not asked to rescan its usage (T3 Code is not running); reading its cache as it is", text)
        self.assertEqual(run.calls, [])

    def test_a_stale_runtime_file_with_nothing_listening_falls_back(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        (self.userdata / "server-runtime.json").write_text(json.dumps({"port": port}))
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("T3 Code is not answering on its port", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])

    def test_a_runtime_file_without_a_port_falls_back(self):
        (self.userdata / "server-runtime.json").write_text(json.dumps({"port": "3773"}))
        result, text = self.refresh(FakeCli())
        self.assertFalse(result)
        self.assertIn("runtime file has no port", text)

    def test_a_missing_command_falls_back(self):
        self.start()
        self.cli_path.unlink()
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("T3 Code's command is not installed", text)
        self.assertEqual(run.calls, [])

    def test_a_command_that_refuses_or_answers_oddly_falls_back_without_revoking(self):
        for run, reason in ((FakeCli(issue_exit=1), "refused to issue a session (exit 1)"),
                            (FakeCli(issue_stdout="Issued bearer access token"), "a form this card does not know"),
                            (FakeCli(issue_stdout=json.dumps({"sessionId": SESSION})), "a form this card does not know")):
            with self.subTest(reason=reason):
                self.start()
                result, text = self.refresh(run)
                self.assertFalse(result)
                self.assertIn(reason, text)
                self.assertEqual(run.verbs, ["issue"])

    def test_a_refused_connection_falls_back_and_revokes(self):
        self.start("unauthorised")
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("T3 Code refused the connection (HTTP 401)", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])

    def test_the_websocket_answer_is_checked_the_rfc_6455_way(self):
        self.assertEqual(refresh.accept_for("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")  # the RFC's own example
        self.start("wrong-accept")
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("T3 Code's WebSocket answer did not match", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])

    def test_a_refused_request_falls_back_and_revokes(self):
        self.start("failure")
        run = FakeCli()
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("T3 Code refused the usage request", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])

    def test_a_t3_that_never_answers_times_out_and_revokes(self):
        self.start("silent")
        run = FakeCli()
        started = time.monotonic()
        result, text = self.refresh(run, timeout_s=0.5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(result)
        self.assertIn("T3 Code did not answer in time", text)
        self.assertEqual(run.verbs, ["issue", "revoke"])

    def test_a_failed_revoke_is_logged_and_the_refresh_still_counts(self):
        self.start()
        result, text = self.refresh(FakeCli(revoke_exit=1))
        self.assertTrue(result)
        self.assertIn("did not revoke the card's session; it expires by itself within 5m", text)

    def test_an_unexpected_error_never_escapes(self):
        self.start()
        run = mock.Mock(side_effect=PermissionError("not allowed " + TOKEN))
        result, text = self.refresh(run)
        self.assertFalse(result)
        self.assertIn("(PermissionError)", text)


class CliRefreshTests(unittest.TestCase):
    """The card is built whatever the refresh step does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.userdata = fx.write_userdata(self.tmp / "userdata")
        self.projects = fx.write_projects(self.tmp / "projects")
        patch = mock.patch.object(cli, "DEFAULT_HISTORY", self.tmp / "default-history.json")
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *extra):
        args = ["--userdata", str(self.userdata), "--claude-projects", str(self.projects), "--log", str(self.tmp / "card.log"),
                "--out", str(self.tmp / "out")] + list(extra)
        return cli.main(args, now=fx.NOW)

    def test_the_card_is_built_when_t3_is_not_running(self):
        self.assertEqual(self.run_cli(), 0)
        log = (self.tmp / "card.log").read_text()
        self.assertIn("T3 Code was not asked to rescan its usage (T3 Code is not running)", log)
        self.assertEqual(len(list((self.tmp / "out").iterdir())), 4)

    def test_the_refresh_runs_before_the_cache_is_read_and_can_be_turned_off(self):
        order, real_build = [], cli.build

        def build(*args, **kwargs):
            order.append("build")
            return real_build(*args, **kwargs)

        with mock.patch.object(cli, "refresh_t3_usage", side_effect=lambda *a, **k: order.append("refresh")) as asked, \
                mock.patch.object(cli, "build", side_effect=build):
            self.assertEqual(self.run_cli(), 0)
            self.assertEqual(order, ["refresh", "build"])
            self.assertEqual(asked.call_args[0], (self.userdata, fx.NOW.astimezone(cli.TIMEZONE).date(), "Asia/Dhaka"))
            order.clear()
            self.assertEqual(self.run_cli("--no-t3-refresh"), 0)
            self.assertEqual(order, ["build"])

if __name__ == "__main__":
    unittest.main()
