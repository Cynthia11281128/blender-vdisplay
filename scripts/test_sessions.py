"""Opt-in real GUI acceptance, isolated paths under workspace tmp."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import pwd
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manage
import control_protocol

BASE_DIR = Path(__file__).resolve().parent.parent
TEST_TMP = Path("/apdcephfs_zwfy7/share_305994131/xyuanzhang/tmp")


def worker(root, argv):
    # Test-only injection: production CLI accepts no arbitrary path overrides.
    root = Path(root).resolve()
    if root.parent != TEST_TMP or not root.name.startswith("bc-"):
        raise ValueError("Invalid test root")
    manage.BASE_DIR = root / "b"
    manage.RUN_HOME = root / "h"
    manage.STATE_ROOT = root / "s"
    original_tmux = manage.Session.tmux
    original_init = manage.Session.__init__
    original_drop = manage.dropped_command
    original_env = manage.Session.env

    def initialize(session, name):
        # Replace only the test runtime before the production path-length guard.
        original_home = manage.RUN_HOME
        manage.RUN_HOME = Path("/var/lib/blender-vdisplay")
        try:
            original_init(session, name)
        finally:
            manage.RUN_HOME = original_home
        session.runtime = root / "r" / name
        session.control_socket = Path(control_protocol.socket_path(session.runtime / "s"))
        session.socket = Path(control_protocol.socket_path(session.runtime / "t"))

    def tmux(session, *args, **kwargs):
        args = list(args)
        if "new-session" in args:
            args[-1] = "exec " + " ".join(shlex.quote(item) for item in
                ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
                 "--worker", str(root), "--supervise", session.name])
        return original_tmux(session, *args, **kwargs)

    def drop(command, environment):
        if str(Path(manage.__file__).resolve()) in command:
            command = ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
                       "--worker", str(root)] + argv
        return original_drop(command, environment)

    def environment(session, metadata=None):
        value = original_env(session, metadata)
        value["PYTHONDONTWRITEBYTECODE"] = "1"
        return value

    manage.Session.tmux = tmux
    manage.Session.__init__ = initialize
    manage.dropped_command = drop
    manage.Session.env = environment
    return manage.main(argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="explicitly start real Blender/Xvfb test sessions")
    args = parser.parse_args()
    if not args.run:
        parser.error("Real GUI test is opt-in; pass --run")
    if os.geteuid() != 0:
        parser.error("Real GUI acceptance requires root")
    account = pwd.getpwnam(manage.RUN_USER)
    root = Path(tempfile.mkdtemp(prefix="bc-", dir=str(TEST_TMP)))
    os.chown(str(root), account.pw_uid, account.pw_gid)
    for name in ("b", "h", "s"):
        directory = root / name
        directory.mkdir(mode=0o700)
        os.chown(str(directory), account.pw_uid, account.pw_gid)
    for name in ("scripts", "blender-4.5.15-linux-x64"):
        (root / "b" / name).symlink_to(BASE_DIR / name)
    first, second = "verify-A-" + uuid.uuid4().hex[:23], "verify-B-" + uuid.uuid4().hex[:23]
    results = {"started_at": datetime.datetime.now().astimezone().isoformat(),
               "root": str(root), "sessions": [first, second], "checks": []}

    def command(*arguments, **kwargs):
        output = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()), "--worker", str(root)] + list(arguments),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=160)
        if output.returncode and not kwargs.get("allow_error"):
            raise RuntimeError("{} failed:\n{}{}".format(arguments, output.stdout, output.stderr))
        return output

    def status(name):
        return json.loads(command("status", name, "--json", allow_error=True).stdout)

    def execute(name, code, timeout=30, ok=True):
        output = command("exec", name, "--code", code, "--timeout", str(timeout), allow_error=True)
        value = json.loads(output.stdout)
        assert value["ok"] is ok, value
        assert output.returncode == (0 if ok else 1), output
        return value

    def clean(name):
        record = status(name)
        if record["state"] == "NOT_STARTED":
            return
        assert record["state"] in ("STOPPED", "FAILED"), record
        assert not Path(record["control"]["socket_path"]).exists()
        for filename in ("Xauthority", "ready.json"):
            assert not (root / "s" / "sessions" / name / filename).exists()
        for child in ("blender", "xvfb", "supervisor"):
            assert not manage.alive(record[child]), record
        display = record["display"][1:]
        assert not Path("/tmp/.X{}-lock".format(display)).exists()
        assert not Path("/tmp/.X11-unix/X{}".format(display)).exists()

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks = [executor.submit(command, "start", name) for name in (first, first, second)]
            results["starts"] = [task.result().stdout for task in tasks]
        a, b = status(first), status(second)
        results["status"] = [a, b]
        assert a["blender"]["pid"] != b["blender"]["pid"]
        assert a["display"] != b["display"]
        assert not set(a["cpus"]) & set(b["cpus"])
        assert a["control"]["socket_path"] != b["control"]["socket_path"]
        for name, record in ((first, a), (second, b)):
            ready = json.loads((root / "s" / "sessions" / name / "ready.json").read_text())
            assert ready["background"] is False and "llvmpipe" in ready["renderer"]
            assert ready["cpus"] == record["cpus"]
            assert any("VIEW_3D" in window["areas"] for window in ready["windows"])
            assert Path(record["control"]["socket_path"]).stat().st_mode & 0o777 == 0o600
            assert (root / "s" / "sessions" / name / "Xauthority").stat().st_mode & 0o777 == 0o600
        assert (root / "s/sessions" / first / "Xauthority").read_bytes() != (root / "s/sessions" / second / "Xauthority").read_bytes()
        results["checks"].append("independent GUI/PID/display/cookie/CPU/socket and concurrent same-name start")
        value = execute(first, "bpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = 'control_probe'\nx = 41\nprint('hello')\nresult = bpy.context.object.name")
        assert value["stdout"] == "hello\n" and value["result"] == "control_probe"
        assert execute(first, "result = [x, 'control_probe' in bpy.data.objects]")["result"] == [41, True]
        assert execute(second, "result = 'control_probe' in bpy.data.objects")["result"] is False
        assert status(first)["blender"] == a["blender"]
        results["checks"].append("same-process object/namespace persistence and cross-session isolation")
        script = root / "private.py"
        script.write_text("import os\nresult = [os.getuid(), os.getcwd(), __name__]\n", encoding="utf-8")
        script.chmod(0o600)
        assert script.stat().st_uid == 0
        output = command("exec", first, "--script", str(script))
        assert json.loads(output.stdout)["result"] == [account.pw_uid, a["project_dir"], "__blenderctl__"]
        results["checks"].append("root-private UTF-8 script read by client; dedicated UID and session cwd preserved")
        for code, kind in (("raise ValueError('probe')", "ValueError"), ("raise SystemExit(2)", "SystemExit")):
            assert execute(first, code, ok=False)["error"]["type"] == kind
        assert execute(first, "result = x")["result"] == 41
        for code, error in (("result = object()", "result_not_serializable"),
                            ("result = float('nan')", "result_not_serializable"),
                            ("print('x' * (1024 * 1024 + 1))", "response_too_large")):
            assert execute(first, code, ok=False)["error"]["code"] == error
        results["checks"].append("exceptions/SystemExit/output overflow/nonserializable result and recovery")
        blend = root / "probe.blend"
        execute(first, "bpy.ops.wm.save_as_mainfile(filepath={!r})".format(str(blend)))
        execute(first, "bpy.data.objects['control_probe'].location.x = 12")
        execute(first, "bpy.ops.wm.open_mainfile(filepath={!r})".format(str(blend)))
        assert execute(first, "result = [x, bpy.data.objects['control_probe'].location.x]")["result"] == [41, 0]
        execute(first, "bpy.ops.wm.read_factory_settings(use_empty=False)")
        assert execute(first, "result = [x, 'control_probe' in bpy.data.objects]")["result"] == [41, False]
        results["checks"].append("save/open_mainfile/factory reset preserve control timer and namespace")
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(execute, first, "import time\ntime.sleep(2)\ny = 7")
            time.sleep(0.5)
            assert execute(first, "pass", ok=False)["error"]["code"] == "busy"
            started = time.monotonic()
            assert execute(second, "result = 19")["result"] == 19
            assert time.monotonic() - started < 1.5
            task.result()
        output = command("exec", first, "--code", "time.sleep(1)\nz = 23", "--timeout", ".3", allow_error=True)
        assert output.returncode and "result unknown" in output.stderr
        assert execute(first, "pass", ok=False)["error"]["code"] == "busy"
        time.sleep(1.2)
        assert execute(first, "result = z")["result"] == 23
        results["checks"].append("busy admission, B executes during A, timeout preserves execution and side effect")
        results["screenshots"] = [command("screenshot", name).stdout.strip() for name in (first, second)]
        for filename in results["screenshots"]:
            dimensions = subprocess.check_output(["/usr/bin/identify", "-format", "%wx%h", filename], universal_newlines=True)
            assert dimensions == "1600x900"
            deviation = subprocess.check_output(["/usr/bin/convert", filename, "-format", "%[fx:standard_deviation]", "info:"],
                                                universal_newlines=True)
            assert float(deviation) > 0.01
        viewport = root / "viewport.png"
        code = """
window = bpy.context.window_manager.windows[0]
area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
region = next(r for r in area.regions if r.type == 'WINDOW')
with bpy.context.temp_override(window=window, area=area, region=region):
    bpy.ops.render.opengl()
bpy.data.images['Render Result'].save_render(%r)
""" % str(viewport)
        execute(first, code, timeout=60)
        assert viewport.stat().st_size > 1000
        results["viewport"] = str(viewport)
        results["checks"].append("nonblank desktop screenshots and OpenGL viewport capture")
        command("stop", first)
        clean(first)
        assert status(second)["blender"] == b["blender"]
        assert execute(second, "result = 31")["result"] == 31
        command("stop", first)
        command("start", first, "--cpus", manage.cpu_text(a["cpus"]))
        new = status(first)
        assert new["generation"] != a["generation"] and new["cpus"] == a["cpus"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(new["control"]["socket_path"])
            old_request = {"protocol_version": 1, "request_id": uuid.uuid4().hex, "generation": a["generation"],
                           "operation": "exec", "filename": "<test>", "code": "old_modified = True"}
            connection.sendall(control_protocol.encode_frame(old_request, control_protocol.MAX_REQUEST))
            value = control_protocol.receive_frame(connection, control_protocol.MAX_RESPONSE, time.monotonic() + 5)
            assert value["error"]["code"] == "generation_mismatch"
        assert execute(first, "result = 'old_modified' in globals()")["result"] is False
        assert status(second)["blender"] == b["blender"]
        results["checks"].append("stop isolated cleanup, idempotence, restart generation guard and explicit CPUs")
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(command, "exec", first, "--code", "import time\ntime.sleep(20)",
                                   allow_error=True)
            time.sleep(0.5)
            started = time.monotonic()
            command("stop", first)
            assert time.monotonic() - started < 12
            output = task.result()
            assert output.returncode != 0 and "result unknown" in output.stderr
        clean(first)
        assert execute(second, "result = 37")["result"] == 37
        results["checks"].append("stop interrupts session during long exec without operation-lock blockage; B survives")
        command("start", first, "--cpus", manage.cpu_text(a["cpus"]))
        output = command("exec", first, "--code", "bpy.ops.wm.quit_blender()", allow_error=True)
        if output.returncode == 0:
            assert json.loads(output.stdout)["ok"] is True
        else:
            assert "result unknown" in output.stderr
        deadline = time.monotonic() + 10
        while status(first)["state"] not in ("STOPPED", "FAILED") and time.monotonic() < deadline:
            time.sleep(.1)
        clean(first)
        command("start", first, "--cpus", manage.cpu_text(a["cpus"]))
        record = status(first)
        assert manage.alive(record["xvfb"]) and record["xvfb"]["uid"] == account.pw_uid
        os.kill(record["xvfb"]["pid"], 15)
        deadline = time.monotonic() + 15
        while status(first)["state"] not in ("STOPPED", "FAILED") and time.monotonic() < deadline:
            time.sleep(.1)
        assert status(first)["state"] == "FAILED"
        clean(first)
        assert execute(second, "result = 43")["result"] == 43
        results["checks"].append("Blender GUI exit and Xvfb failure clean own socket/processes; B remains usable")
    except BaseException as error:
        results["failure"] = repr(error)
        raise
    finally:
        errors = []
        for name in (first, second):
            try:
                command("stop", name)
                clean(name)
            except BaseException as error:
                errors.append("{}: {}".format(name, error))
        results["cleanup_errors"] = errors
        results["finished_at"] = datetime.datetime.now().astimezone().isoformat()
        (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))
        if errors:
            raise RuntimeError("Test cleanup failed: " + "; ".join(errors))


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
            sys.exit(worker(sys.argv[2], sys.argv[3:]))
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("test_sessions: {}".format(error), file=sys.stderr)
        sys.exit(1)
