"""Opt-in real acceptance of sandbox sessions. Root only; starts and stops its own sessions.

/usr/bin/python3 -I -B scripts/test_sandbox_live.py --run
Uses the deployed CLI and random `sbxtest-` names; inputs live under workspace tmp.
"""

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

BASE_DIR = Path(__file__).resolve().parent.parent
CLI = str(BASE_DIR / "blenderctl")
TEST_TMP = Path("/apdcephfs_zwfy7/share_305994131/xyuanzhang/tmp")
STATE_ROOT = Path("/var/lib/blender-vdisplay/.local/state/blender-virtual")


def cli(*arguments, check=True):
    result = subprocess.run([CLI] + list(arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=240)
    if check and result.returncode:
        raise AssertionError("{} failed: {}".format(arguments, result.stderr))
    return result


def status(name):
    return json.loads(cli("status", name, "--json", check=False).stdout)


def execute(name, code):
    return json.loads(cli("exec", name, "--code", code, "--timeout", "60", check=False).stdout)


def wait_state(name, states, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status(name)["state"] in states:
            return
        time.sleep(0.2)
    raise AssertionError("{} did not reach {}".format(name, states))


def namespace_processes(record):
    """Host pids still alive whose ancestry includes the recorded unshare process."""
    root = record["unshare"]["pid"]
    alive = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        pid = int(path.name)
        while pid > 1:
            try:
                pid = int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, ProcessLookupError, ValueError):
                break
            if pid == root:
                alive.append(int(path.name))
                break
    return alive


PROBE = """
import os, socket, tempfile
r = {{}}
def attempt(name, fn):
    try:
        fn(); r[name] = "reached"
    except BaseException as error:
        r[name] = type(error).__name__
attempt("hidden", lambda: os.listdir({hidden!r}))
attempt("state_root", lambda: os.listdir({state_root!r}))
attempt("other_socket", lambda: socket.socket(socket.AF_UNIX).connect({other_socket!r}))
attempt("host_port", lambda: socket.create_connection(("127.0.0.1", {port}), 2))
attempt("network", lambda: socket.create_connection(("8.8.8.8", 53), 2))
attempt("write_read_only", lambda: open({read_only!r} + "/x", "w"))
attempt("etc_secret", lambda: os.listdir("/etc/taiji"))
r["read"] = open({read_only!r} + "/input.txt").read()
open({read_write!r} + "/written.txt", "w").write("ok")
r["tmpdir"] = tempfile.gettempdir()
r["pids"] = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
r["sessions_visible"] = os.listdir({state_root!r} + "/sessions")
result = r
"""


def run():
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    account = pwd.getpwnam("blender-vdisplay")
    root = Path(tempfile.mkdtemp(prefix="sbxtest-", dir=str(TEST_TMP)))
    tag = uuid.uuid4().hex[:8]
    names = ["sbxtest-{}-{}".format(tag, index) for index in range(3)]
    other = "sbxtest-{}-plain".format(tag)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    results = {"root": str(root), "checks": {}}
    try:
        root.chmod(0o755)  # mkdtemp is 0700; the account must traverse to the bound paths
        (root / "ro").mkdir(mode=0o755)
        (root / "ro/input.txt").write_text("hello")
        (root / "ro/input.txt").chmod(0o644)
        (root / "hidden").mkdir()
        for directory in ("rw", "rw/tmp"):
            (root / directory).mkdir()
            os.chown(str(root / directory), account.pw_uid, account.pw_gid)
        spec = root / "spec.json"
        spec.write_text(json.dumps({"schema_version": 1, "read_only": [str(root / "ro")],
                                    "read_write": [str(root / "rw")], "tmpdir": str(root / "rw/tmp")}))
        checks = results["checks"]

        cli("start", other, "--cpu-count", "2")
        started = time.monotonic()
        cli("start", names[0], "--sandbox", str(spec), "--cpu-count", "2")
        checks["start_seconds"] = round(time.monotonic() - started, 2)
        state = status(names[0])
        assert state["state"] == "RUNNING" and state["control"]["ready"] and state["sandbox"], state
        probe = execute(names[0], PROBE.format(
            hidden=str(root / "hidden"), state_root=str(STATE_ROOT), port=listener.getsockname()[1],
            other_socket=status(other)["control"]["socket_path"], read_only=str(root / "ro"),
            read_write=str(root / "rw")))
        assert probe["ok"], probe
        value = probe["result"]
        checks["probe"] = value
        for key in ("hidden", "other_socket", "host_port", "network", "write_read_only", "etc_secret"):
            assert value[key] != "reached", key
        assert value["read"] == "hello" and (root / "rw/written.txt").read_text() == "ok"
        assert value["tmpdir"] == str(root / "rw/tmp")
        assert value["sessions_visible"] == [names[0]], value["sessions_visible"]
        assert len(value["pids"]) <= 4, value["pids"]
        for command in (("screenshot", names[0]), ("attach", names[0])):
            assert cli(*command, check=False).returncode, command

        generation = state["generation"]
        assert cli("stop", names[0], "--generation", "wrong", check=False).returncode
        assert status(names[0])["state"] == "RUNNING"
        record = json.loads((STATE_ROOT / "sandbox" / "{}.json".format(names[0])).read_text())
        started = time.monotonic()
        cli("stop", names[0], "--generation", generation)
        checks["stop_seconds"] = round(time.monotonic() - started, 2)
        assert status(names[0])["state"] == "STOPPED" and not namespace_processes(record)

        cli("start", names[1], "--sandbox", str(spec), "--cpu-count", "2")
        record = json.loads((STATE_ROOT / "sandbox" / "{}.json".format(names[1])).read_text())
        os.kill(record["supervisor"]["pid"], signal.SIGKILL)
        wait_state(names[1], ("STOPPED", "FAILED"))
        time.sleep(1)
        assert not namespace_processes(record)
        checks["killed_supervisor"] = "cleaned"

        cli("start", names[2], "--sandbox", str(spec), "--cpu-count", "2")
        record = json.loads((STATE_ROOT / "sandbox" / "{}.json".format(names[2])).read_text())
        # Blender dies mid-request, so the client reports a transport failure, not JSON.
        assert cli("exec", names[2], "--code", "import os; os._exit(3)", "--timeout", "10", check=False).returncode
        wait_state(names[2], ("FAILED",))
        time.sleep(1)
        assert not namespace_processes(record)
        checks["blender_crash"] = status(names[2])["error"]

        assert status(other)["state"] == "RUNNING" and "sandbox" not in status(other)
        results["status"] = "passed"
    finally:
        listener.close()
        for name in names + [other]:
            state = status(name)
            if state["state"] not in ("NOT_STARTED", "STOPPED", "FAILED"):
                cli("stop", name, check=False)
        shutil.rmtree(str(root), ignore_errors=True)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="actually start sessions")
    if not parser.parse_args().run:
        raise SystemExit("pass --run to start real sessions")
    run()
