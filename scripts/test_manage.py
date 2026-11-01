import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("manage", Path(__file__).with_name("manage.py"))
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)
TEST_TMP = Path("/apdcephfs_zwfy7/share_305994131/xyuanzhang/tmp")


class ManagementTests(unittest.TestCase):
    def test_names_reject_paths_and_shell_syntax(self):
        for name in ("", "../A", "/tmp/A", "a b", "A;touch", "A" * 33, "-A"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                manage.valid_name(name)
        for name in ("default", "A", "B-2", "asset_review"):
            self.assertEqual(manage.valid_name(name), name)

    def test_cpu_allocation_and_validation(self):
        allowed = set(range(24))
        self.assertEqual(manage.select_cpus(allowed, set(range(8))), list(range(8, 16)))
        self.assertEqual(manage.select_cpus(allowed, set(), "2,4-6"), [2, 4, 5, 6])
        for value in ("", "2-1", "24", "-1", "0;1", "0-1000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manage.parse_cpus(value, allowed)
        with self.assertRaises(ValueError):
            manage.select_cpus(set(range(4)), set())

    def test_pid_reuse_guard(self):
        record = manage.identity(os.getpid())
        self.assertTrue(manage.alive(record))
        wrong = dict(record, start_time="not-the-original-process")
        self.assertFalse(manage.alive(wrong))

    def test_per_session_paths(self):
        first, second = manage.Session("A"), manage.Session("B")
        for attribute in ("state", "socket", "control_socket", "config", "project", "shots", "runtime"):
            self.assertNotEqual(getattr(first, attribute), getattr(second, attribute))
        self.assertLess(len(str(manage.Session("A" * 32).socket)), 108)
        self.assertEqual(len(os.fsencode(manage.Session("A" * 32).control_socket)), 102)

    def test_json_and_generation_guard(self):
        with tempfile.TemporaryDirectory(dir=str(TEST_TMP)) as root, mock.patch.object(manage, "STATE_ROOT", Path(root)):
            session = manage.Session("A")
            session.state.mkdir(parents=True)
            record = {"token": "old", "pid": 123, "uid": 992}
            manage.write_json(session.state / "ready.json", record)
            metadata = {"token": "old", "blender": {"pid": 123, "uid": 992}}
            self.assertIsNone(session.ready(dict(metadata, token="new")))
            self.assertIsNone(session.ready(dict(metadata, blender={"pid": 124, "uid": 992})))
            self.assertIsNone(session.ready(dict(metadata, blender={"pid": 123, "uid": 993})))
            self.assertEqual(session.ready(metadata), record)
            self.assertEqual(list(session.state.iterdir()), [session.state / "ready.json"])

    def test_allocation_skips_held_display_lock(self):
        with tempfile.TemporaryDirectory(dir=str(TEST_TMP)) as root, mock.patch.object(manage, "STATE_ROOT", Path(root)):
            locks = Path(root) / "displays"
            locks.mkdir()
            with manage.lock(locks / "99.lock"):
                self.assertGreater(manage.allocate(), 99)

    def test_stale_supervisor_does_not_receive_signal(self):
        with mock.patch.object(manage, "alive", return_value=False), mock.patch.object(manage.os, "kill") as kill:
            with self.assertRaises(RuntimeError):
                manage.send_stop({"supervisor": {"pid": 123, "start_time": "old", "uid": 992}})
            kill.assert_not_called()

    def test_stale_ready_does_not_mark_session_running(self):
        session = manage.Session("A")
        metadata = {"status": "running", "supervisor": None, "blender": None, "xvfb": None}
        with mock.patch.object(manage, "read_json", return_value=metadata):
            self.assertEqual(session.status()[0], "STOPPED")

    def test_json_status_is_one_document_and_no_gui_subprocess(self):
        session = manage.Session("A")
        for state in ("NOT_STARTED", "STOPPED", "RUNNING"):
            metadata = None if state == "NOT_STARTED" else {"token": "g", "display_number": 99}
            with mock.patch.object(session, "status", return_value=(state, metadata)), \
                    mock.patch.object(session, "control_ready", return_value=True), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output, \
                    mock.patch.object(manage.subprocess, "run") as run:
                self.assertEqual(manage.status(session, True), 0 if state == "RUNNING" else 1)
                value = json.loads(output.getvalue())
                self.assertEqual(value["state"], state)
                self.assertEqual(value["control"]["ready"], state == "RUNNING")
                self.assertNotIn("Xauthority", output.getvalue())
                run.assert_not_called()

    def test_exec_cli_rejects_invalid_arguments(self):
        for args in (["exec"], ["exec", "--code", "pass", "--script", "x"],
                     ["status", "--code", "pass"], ["exec", "--code", "pass", "--json"],
                     ["exec", "--code", "pass", "--timeout", "nan"],
                     ["exec", "--code", "pass", "--timeout", "inf"],
                     ["exec", "--code", "pass", "--timeout", "0"],
                     ["exec", "--code", "pass", "--timeout", "-1"]):
            with self.subTest(args=args), mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    manage.main(args)

    def test_root_exec_is_not_forwarded_and_never_locks(self):
        account = mock.Mock(pw_uid=992)
        with mock.patch.object(manage.pwd, "getpwnam", return_value=account), \
                mock.patch.object(manage.os, "geteuid", return_value=0), \
                mock.patch.object(manage.os, "execv") as forward, \
                mock.patch.object(manage, "execute", return_value=0) as execute:
            self.assertEqual(manage.main(["exec", "A", "--code", "pass"]), 0)
            execute.assert_called_once()
            forward.assert_not_called()

    def test_script_utf8_limit_and_caller_filename(self):
        with tempfile.TemporaryDirectory(dir=str(TEST_TMP)) as root:
            script = Path(root) / "private.py"
            script.write_bytes(b"result = 42")
            session = manage.Session("A")
            with mock.patch.object(session, "status", return_value=("RUNNING", {"token": "g"})), \
                    mock.patch.object(session, "control_ready", return_value=True), \
                    mock.patch.object(manage.control_client, "execute", return_value={"ok": True}) as execute, \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                manage.execute(session, script=str(script))
                self.assertEqual(execute.call_args[0][2], "result = 42")
                self.assertEqual(execute.call_args[0][4], str(script))
                script.write_bytes(b"\xff")
                with self.assertRaises(UnicodeError):
                    manage.execute(session, script=str(script))
                script.write_bytes(b" " * (manage.control_protocol.MAX_CODE + 1))
                with self.assertRaises(ValueError):
                    manage.execute(session, script=str(script))

    def test_stopped_exec_does_not_start_or_connect(self):
        session = manage.Session("A")
        with mock.patch.object(session, "status", return_value=("STOPPED", {})), \
                mock.patch.object(manage.control_client, "execute") as execute:
            with self.assertRaises(RuntimeError):
                manage.execute(session, code="pass")
            execute.assert_not_called()

    def test_control_cleanup_is_generation_scoped(self):
        session = manage.Session("A")
        session.control_socket = mock.Mock()
        with mock.patch.object(manage, "read_json", return_value={"token": "new"}):
            session.cleanup_control({"token": "old"})
            session.control_socket.unlink.assert_not_called()
            session.cleanup_control({"token": "new"})
            session.control_socket.unlink.assert_called_once()

    def test_existing_legacy_session_requires_explicit_restart(self):
        session = manage.Session("A")
        with mock.patch.object(session, "prepare"), \
                mock.patch.object(manage, "lock", return_value=mock.MagicMock()), \
                mock.patch.object(session, "status", return_value=("RUNNING", {"token": "g"})), \
                mock.patch.object(session, "control_ready", return_value=False), \
                mock.patch.object(session, "tmux") as tmux:
            with self.assertRaisesRegex(RuntimeError, "stop and restart"):
                manage.start(session, None)
            tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
