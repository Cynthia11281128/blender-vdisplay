import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sandbox = load("sandbox")
manage = load("manage")
TEST_TMP = Path("/apdcephfs_zwfy7/share_305994131/xyuanzhang/tmp")
UID = os.getuid()


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=str(TEST_TMP))
        self.root = Path(self.temporary.name).resolve()
        for name in ("task", "out", "out/tmp", "protected"):
            (self.root / name).mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def spec(self, **changes):
        value = {"schema_version": 1, "read_only": [str(self.root / "task")],
                 "read_write": [str(self.root / "out")], "tmpdir": str(self.root / "out/tmp")}
        value.update(changes)
        path = self.root / "spec.json"
        path.write_text(json.dumps(value))
        return path

    def load(self, **changes):
        return sandbox.load_spec(self.spec(**changes), UID, [self.root / "protected"])

    def test_valid_spec(self):
        spec = self.load()
        self.assertEqual(spec["read_only"], [str(self.root / "task")])
        self.assertEqual(spec["tmpdir"], str(self.root / "out/tmp"))

    def test_rejections(self):
        cases = [
            {"extra": 1}, {"schema_version": 2}, {"schema_version": True}, {"read_only": "x"},
            {"read_only": ["relative"]}, {"read_only": [str(self.root / "missing")]},
            {"read_only": ["/"]}, {"read_only": ["/etc"]}, {"read_only": ["/etc/taiji"]},
            {"read_only": [str(self.root)]}, {"read_only": [str(self.root / "protected")]},
            {"read_only": [str(self.root / "out")]},
            {"read_only": [str(self.root / "task"), str(self.root / "task")]},
            {"read_only": [str(self.root / "task") + "/../task"]},
            {"tmpdir": str(self.root / "task")},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(sandbox.SpecError):
                self.load(**changes)
        with self.assertRaises(sandbox.SpecError):
            sandbox.load_spec(self.spec(), UID + 1, [])

    def test_symlink_is_rejected(self):
        os.symlink(str(self.root / "task"), str(self.root / "link"))
        with self.assertRaises(sandbox.SpecError):
            self.load(read_only=[str(self.root / "link")])


class PlanTests(unittest.TestCase):
    def plan(self):
        spec = {"read_only": ["/data/task"], "read_write": ["/data/out"], "tmpdir": "/data/out/tmp"}
        return sandbox.mount_plan(spec, "/opt/blender", "/opt/scripts", ["/home/acct/state"],
                                  "/home/acct", "/home/acct/state/displays", (992, 989))

    def test_whitelist_contents(self):
        plan = self.plan()
        binds = {entry["target"]: entry for entry in plan if entry["kind"] == "bind"}
        self.assertTrue(binds["/usr"]["read_only"])
        self.assertTrue(binds["/opt/blender"]["read_only"] and binds["/data/task"]["read_only"])
        self.assertFalse(binds["/data/out"]["read_only"] or binds["/home/acct/state"]["read_only"])
        self.assertNotIn("/etc", binds)
        self.assertNotIn("/etc/taiji", binds)
        for entry in plan:
            self.assertNotIn(entry["target"], ("/apdcephfs_zwfy7", "/var", "/sys", "/root"))

    def test_parents_are_mounted_before_children(self):
        plan = self.plan()
        position = {entry["target"]: index for index, entry in enumerate(plan)}
        self.assertLess(position["/home/acct"], position["/home/acct/state"])
        self.assertLess(position["/tmp"], position["/tmp/.X11-unix"])
        self.assertLess(position["/dev"], position["/dev/null"])


class HostStateTests(unittest.TestCase):
    def test_dead_sandbox_never_checks_namespace_pids(self):
        session = manage.Session("A")
        metadata = {"status": "running", "token": "t", "sandbox": {"tmpdir": "/x"},
                    "supervisor": {"pid": 1, "start_time": "1", "uid": 992}}
        with mock.patch.object(manage, "read_json", side_effect=lambda path: None
                               if path == session.sandbox_record else metadata), \
                mock.patch.object(manage, "alive", side_effect=AssertionError("pid checked")):
            self.assertEqual(session.status()[0], "STOPPED")

    def test_live_sandbox_status_and_generation_come_from_root_record(self):
        session = manage.Session("A")
        record = {"token": "good", "unshare": {"pid": 5, "start_time": "9"}}
        metadata = {"status": "running", "token": "forged", "supervisor": {"pid": 1}}
        with mock.patch.object(manage, "read_json", side_effect=lambda path: record
                               if path == session.sandbox_record else metadata), \
                mock.patch.object(manage, "host_alive", return_value=True):
            self.assertEqual(session.status()[0], "STOPPING")
            self.assertEqual(session.generation(metadata), "good")
            metadata["token"] = "good"
            self.assertEqual(session.status()[0], "RUNNING")

    def test_stop_generation_mismatch_sends_no_signal(self):
        with tempfile.TemporaryDirectory(dir=str(TEST_TMP)) as root, \
                mock.patch.object(manage, "STATE_ROOT", Path(root)):
            session = manage.Session("A")
            session.state.mkdir(parents=True)
            with mock.patch.object(session, "status", return_value=("RUNNING", {"token": "a"})), \
                    mock.patch.object(session, "live_sandbox", return_value=None), \
                    mock.patch.object(manage.os, "kill") as kill:
                with self.assertRaises(RuntimeError):
                    manage.stop(session, "b")
                kill.assert_not_called()

    def test_tmpdir_environment_only_for_sandbox(self):
        self.assertNotIn("TMPDIR", manage.Session("A").env({"threads": 8}))
        env = manage.Session("A").env({"threads": 8, "sandbox": {"tmpdir": "/x/tmp"}})
        self.assertEqual((env["TMPDIR"], env["TMP"], env["TEMP"]), ("/x/tmp",) * 3)

    def test_cpu_count(self):
        self.assertEqual(manage.select_cpus(set(range(24)), set(range(8)), None, 2), [8, 9])
        with self.assertRaises(ValueError):
            manage.select_cpus(set(range(4)), set(), None, 5)

    def test_cli_argument_scoping(self):
        for argv in (["stop", "A", "--sandbox", "x"], ["status", "A", "--cpu-count", "2"],
                     ["start", "A", "--generation", "x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr"):
                manage.main(argv)


if __name__ == "__main__":
    unittest.main()
