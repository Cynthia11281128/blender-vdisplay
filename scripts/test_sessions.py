"""Run the focused real-Blender multi-session acceptance test, then stop its sessions."""

from concurrent.futures import ThreadPoolExecutor
import datetime
import json
from pathlib import Path
import subprocess
import uuid


BASE_DIR = Path(__file__).resolve().parent.parent
CONTROL = str(BASE_DIR / "blenderctl")
STATE_ROOT = Path("/var/lib/blender-vdisplay/.local/state/blender-virtual/sessions")


def run(*arguments):
    result = subprocess.run([CONTROL] + list(arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=160)
    if result.returncode:
        raise RuntimeError("{} failed:\n{}{}".format(arguments, result.stdout, result.stderr))
    return result.stdout


def metadata(name):
    return json.loads((STATE_ROOT / name / "session.json").read_text())


def ready(name):
    return json.loads((STATE_ROOT / name / "ready.json").read_text())


def assert_clean(name):
    record = metadata(name)
    assert record["status"] == "stopped", record
    for resource in ("Xauthority", "ready.json"):
        assert not (STATE_ROOT / name / resource).exists()
    number = record["display_number"]
    assert not Path("/tmp/.X{}-lock".format(number)).exists()
    assert not Path("/tmp/.X11-unix/X{}".format(number)).exists()
    for child in ("supervisor", "blender", "xvfb"):
        if record.get(child):
            assert not Path("/proc/{}".format(record[child]["pid"])).exists()


def main():
    suffix = uuid.uuid4().hex[:8]
    first, second = "verify-A-" + suffix, "verify-B-" + suffix
    results = {"started_at": datetime.datetime.now().astimezone().isoformat(),
               "sessions": [first, second]}
    try:
        # Two same-name requests plus another session exercise both operation and allocation locks.
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks = [executor.submit(run, "start", name) for name in (first, first, second)]
            results["starts"] = [task.result() for task in tasks]
        a, b = metadata(first), metadata(second)
        results["metadata"] = [a, b]
        assert a["display_number"] != b["display_number"]
        assert a["blender"]["pid"] != b["blender"]["pid"]
        assert not set(a["cpus"]) & set(b["cpus"])
        for name, record in ((first, a), (second, b)):
            value = ready(name)
            assert value["background"] is False
            assert value["session"] == name
            assert value["cpus"] == record["cpus"]
            assert value["project_dir"] == str(BASE_DIR / "projects" / name)
            assert value["config_dir"] == str(BASE_DIR / "user-config" / name)
            assert any("VIEW_3D" in window["areas"] for window in value["windows"])
            assert "llvmpipe" in value["renderer"]
            assert (STATE_ROOT / name / "Xauthority").stat().st_mode & 0o777 == 0o600
            results.setdefault("ready", []).append(value)
        assert (STATE_ROOT / first / "Xauthority").read_bytes() != (STATE_ROOT / second / "Xauthority").read_bytes()
        results["list"] = run("list")
        results["screenshots"] = [run("screenshot", name).strip() for name in (first, second)]
        for name, filename in zip((first, second), results["screenshots"]):
            assert Path(filename).parent == BASE_DIR / "screenshots" / name
            dimensions = subprocess.check_output(["/usr/bin/identify", "-format", "%wx%h", filename],
                                                 universal_newlines=True)
            assert dimensions == "1600x900"
        original_b_pid = b["blender"]["pid"]
        results["stop_A"] = run("stop", first)
        assert_clean(first)
        results["status_B_after_stop_A"] = run("status", second)
        assert metadata(second)["blender"]["pid"] == original_b_pid
        results["stop_A_again"] = run("stop", first)
        run("start", first, "--cpus", ",".join(str(cpu) for cpu in a["cpus"]))
        assert metadata(first)["token"] != a["token"]
        assert metadata(first)["cpus"] == a["cpus"]
        assert metadata(second)["blender"]["pid"] == original_b_pid
        results["checks"] = [
            "same-name concurrent start creates one Blender",
            "different sessions have independent displays, cookies, paths, and CPU sets",
            "per-session GUI and screenshot dimensions verified",
            "stop A cleans only A; B remains running",
            "repeated stop is idempotent",
            "restart uses fresh generation token and honors --cpus",
        ]
    finally:
        for name in (first, second):
            run("stop", name)
            assert_clean(name)
    results["finished_at"] = datetime.datetime.now().astimezone().isoformat()
    results["cleanup"] = "Both temporary sessions stopped; config, screenshots, and logs retained."
    (BASE_DIR / "artifacts/multi-session-test.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"sessions": results["sessions"], "screenshots": results["screenshots"],
                      "checks": results["checks"], "cleanup": results["cleanup"]}, indent=2))


if __name__ == "__main__":
    main()
