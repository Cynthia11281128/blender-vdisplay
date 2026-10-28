import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("manage", Path(__file__).with_name("manage.py"))
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


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
        for attribute in ("state", "socket", "config", "project", "shots", "runtime"):
            self.assertNotEqual(getattr(first, attribute), getattr(second, attribute))
        self.assertLess(len(str(manage.Session("A" * 32).socket)), 108)

    def test_json_and_generation_guard(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(manage, "STATE_ROOT", Path(root)):
            session = manage.Session("A")
            session.state.mkdir(parents=True)
            manage.write_json(session.state / "ready.json", {"token": "old"})
            self.assertIsNone(session.ready({"token": "new"}))
            self.assertEqual(session.ready({"token": "old"}), {"token": "old"})
            self.assertEqual(list(session.state.iterdir()), [session.state / "ready.json"])

    def test_allocation_skips_held_display_lock(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(manage, "STATE_ROOT", Path(root)):
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


if __name__ == "__main__":
    unittest.main()
