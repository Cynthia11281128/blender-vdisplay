"""Offline protocol, execution and actual Unix socket tests; no Blender process."""

import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_client as client
import control_protocol as protocol
import control_server as server

TEST_TMP = "/apdcephfs_zwfy7/share_305994131/xyuanzhang/tmp"


def request(code="result = 42", generation="g"):
    return dict(protocol_version=1, request_id="r", generation=generation,
                operation="exec", filename="/caller/task.py", code=code)


class ProtocolTests(unittest.TestCase):
    def test_request_validation(self):
        for value in ([], {}, dict(request(), protocol_version=True),
                      dict(request(), protocol_version=2), dict(request(), operation="shell"),
                      dict(request(), code=4), dict(request(), generation=""),
                      dict(request(), code="x" * (protocol.MAX_CODE + 1))):
            with self.subTest(value=str(value)[:80]), self.assertRaises(protocol.ProtocolError):
                protocol.validate_request(value)
        protocol.validate_request(request())

    def test_frame_bounds_utf8_nan_and_truncation(self):
        for payload in (b"\xff\n", b"NaN\n", b"1e400\n", b"{}\n{}\n", b"xx\n", b"x" * 100, b'{"x":'):
            a, b = socket.socketpair()
            with a, b:
                b.sendall(payload)
                b.shutdown(socket.SHUT_WR)
                with self.assertRaises(protocol.ProtocolError):
                    protocol.receive_frame(a, 64, time.monotonic() + 1)
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_frame({"x": "x" * 100}, 32)
        with self.assertRaises(ValueError):
            protocol.encode_frame({"x": float("nan")}, 100)
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_frame({"x": 2 ** 10000}, 100)

    def test_response_identity_and_shape(self):
        value = protocol.response(request(), "g")
        self.assertEqual(protocol.validate_response(value, request()), value)
        for change in ({"request_id": "other"}, {"generation": "old"}, {"ok": 1},
                       {"protocol_version": 2}, {"duration_ns": -1}, {"stdout": []},
                       {"ok": False}, {"ok": False, "error": {}}):
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_response(dict(value, **change), request())

    def test_path_byte_limit(self):
        protocol.socket_path("/" + "a" * 106)
        for path in ("relative", "/" + "a" * 107, "/" + "\u00e9" * 54):
            with self.assertRaises(ValueError):
                protocol.socket_path(path)

    def test_timeout_without_data(self):
        a, b = socket.socketpair()
        with a, b, self.assertRaises((socket.timeout, TimeoutError)):
            protocol.receive_frame(a, 100, time.monotonic() + 0.01)


class ExecutionTests(unittest.TestCase):
    def execute(self, code, namespace=None):
        frame, value = server.execute_request(request(code), "g", namespace if namespace is not None else {})
        self.assertEqual(json.loads(frame), value)
        return value

    def test_persistent_namespace_and_result_reset(self):
        namespace = {"__name__": "__blenderctl__"}
        self.assertTrue(self.execute("x = 9\nresult = x\nprint('hello')", namespace)["ok"])
        self.assertIsNone(self.execute("x += 1", namespace)["result"])
        self.assertEqual(self.execute("result = x", namespace)["result"], 10)

    def test_exceptions_systemexit_and_no_rollback(self):
        namespace = {}
        value = self.execute("x = 7\nraise ValueError('bad')", namespace)
        self.assertEqual(value["error"]["code"], "execution_error")
        self.assertIn("/caller/task.py", value["error"]["traceback"])
        self.assertEqual(self.execute("raise SystemExit(2)", namespace)["error"]["type"], "SystemExit")
        self.assertEqual(self.execute("result = x", namespace)["result"], 7)
        value = self.execute("class Bad(Exception):\n def __str__(self): raise SystemExit()\nraise Bad()")
        self.assertEqual(value["error"]["message"], "<exception message unavailable>")

    def test_result_serialization(self):
        for code in ("result = object()", "result = float('nan')", "result = float('inf')",
                     "result = []\nresult.append(result)"):
            self.assertEqual(self.execute(code)["error"]["code"], "result_not_serializable")
        self.assertEqual(self.execute("result = 'x' * (5 * 1024 * 1024)")["error"]["code"], "response_too_large")

    def test_shared_output_budget_and_unicode(self):
        value = self.execute("import sys\nsys.stdout.write('\\x00' * (1024 * 1024))")
        self.assertEqual(value["error"]["code"], "response_too_large")
        self.assertEqual(value["stdout"], "")
        with mock.patch.object(protocol, "MAX_OUTPUT", 12):
            value = self.execute("import sys\nprint('12345678')\nsys.stderr.write('abcdef')")
            self.assertFalse(value["ok"])
            self.assertEqual(value["error"]["code"], "response_too_large")
            self.assertLessEqual(len((value["stdout"] + value["stderr"]).encode("utf-8")), 12)
            value = self.execute("print('\\u00e9' * 1000000)")
            self.assertFalse(value["ok"])
            self.assertLessEqual(len(value["stdout"].encode("utf-8")), 12)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bc-", dir=TEST_TMP)
        self.path = str(Path(self.directory.name) / "s")
        self.bpy = mock.Mock()
        self.service = server.ControlServer(self.path, "g", self.bpy).start()
        self.stop = threading.Event()
        self.ticker = None

    def start_ticker(self):
        def tick():
            while not self.stop.wait(0.005):
                self.service.tick()
        self.ticker = threading.Thread(target=tick)
        self.ticker.start()

    def tearDown(self):
        self.stop.set()
        if self.ticker:
            self.ticker.join(3)
            self.assertFalse(self.ticker.is_alive())
        self.service.close()
        self.service.accept_thread.join(2)
        self.assertFalse(self.service.accept_thread.is_alive())
        self.assertFalse(Path(self.path).exists())
        self.directory.cleanup()

    def raw(self, value):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(self.path)
            connection.sendall(protocol.encode_frame(value, protocol.MAX_REQUEST))
            return protocol.receive_frame(connection, protocol.MAX_RESPONSE, time.monotonic() + 2)

    def test_success_permissions_and_persistent_timer(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.bpy.app.timers.register.assert_called_once_with(self.service.tick, first_interval=0.05, persistent=True)
        self.start_ticker()
        self.assertEqual(client.execute(self.path, "g", "result = 23")["result"], 23)

    def test_generation_invalid_and_permission_rejection(self):
        self.assertEqual(self.raw(request(generation="old"))["error"]["code"], "generation_mismatch")
        self.assertEqual(self.raw(dict(request(), protocol_version=7))["error"]["code"], "invalid_request")
        with mock.patch.object(server.struct, "unpack", return_value=(123, 123456789, 0)):
            with self.assertRaises((OSError, protocol.ProtocolError)):
                self.raw(request())

    def test_busy_disconnect_cancels_pending(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(self.path)
        connection.sendall(protocol.encode_frame(request(), protocol.MAX_REQUEST))
        deadline = time.monotonic() + 2
        while self.service.slot is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(self.service.slot)
        self.assertEqual(self.raw(request())["error"]["code"], "busy")
        connection.close()
        deadline = time.monotonic() + 2
        while self.service.slot is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(self.service.slot)
        self.start_ticker()
        self.assertEqual(client.execute(self.path, "g", "result = 1")["result"], 1)

    def test_started_timeout_keeps_slot_and_side_effect(self):
        self.start_ticker()
        with self.assertRaisesRegex(client.ControlClientError, "result unknown"):
            client.execute(self.path, "g", "import time\ntime.sleep(.3)\nx = 17", timeout=0.08)
        self.assertEqual(self.raw(request())["error"]["code"], "busy")
        time.sleep(0.35)
        self.assertEqual(client.execute(self.path, "g", "result = x")["result"], 17)

    def test_transport_failure_and_response_mismatch(self):
        with self.assertRaises(client.ControlClientError):
            client.execute(self.path + "-missing", "g", "pass")
        self.start_ticker()
        with mock.patch.object(protocol, "validate_response", side_effect=protocol.ProtocolError("wrong ID")):
            with self.assertRaisesRegex(client.ControlClientError, "result unknown"):
                client.execute(self.path, "g", "pass")


if __name__ == "__main__":
    unittest.main()
