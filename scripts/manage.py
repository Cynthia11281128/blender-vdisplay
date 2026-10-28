import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid


BASE_DIR = Path(__file__).resolve().parent.parent
RUN_USER = "blender-vdisplay"
RUN_HOME = Path("/var/lib/blender-vdisplay")
STATE_ROOT = RUN_HOME / ".local/state/blender-virtual"
START_TIMEOUT = 120
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")


def valid_name(value):
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError("Session names must be 1-32 letters/digits/_/-, starting with a letter or digit.")
    return value


def timestamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    try:
        with path.open() as source:
            return json.load(source)
    except FileNotFoundError:
        return None


def write_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False) as target:
        temporary = Path(target.name)
        json.dump(value, target, indent=2)
        target.write("\n")
    try:
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def lock(path, blocking=True):
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield descriptor
    finally:
        os.close(descriptor)


def identity(pid):
    try:
        raw = Path("/proc/{}/stat".format(pid)).read_text()
        fields = raw.rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start_time": fields[19],
                "uid": Path("/proc/{}".format(pid)).stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record):
    return bool(record and identity(record["pid"]) == record)


def parse_cpus(value, allowed):
    selected = set()
    for part in value.split(","):
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError("Invalid --cpus list; use e.g. 0-7 or 0,2,4-7.")
        bounds = [int(number) for number in part.split("-")]
        first, last = bounds[0], bounds[-1]
        if first > last or last > max(allowed):
            raise ValueError("CPU range is reversed or outside the allowed CPUs.")
        selected.update(range(first, last + 1))
    if not selected or not selected.issubset(allowed):
        raise ValueError("Requested CPUs are not in this process's allowed CPU set.")
    return sorted(selected)


def select_cpus(allowed, used, requested=None):
    if requested is not None:
        return parse_cpus(requested, allowed)
    available = sorted(allowed - used)
    if len(available) < 8:
        raise ValueError("Fewer than 8 unassigned logical CPUs; specify --cpus explicitly or stop a session.")
    return available[:8]


def cpu_text(cpus):
    return ",".join(str(cpu) for cpu in cpus)


class Session:
    def __init__(self, name):
        self.name = valid_name(name)
        self.state = STATE_ROOT / "sessions" / name
        self.metadata = self.state / "session.json"
        self.socket = self.state / "tmux.sock"
        self.config = BASE_DIR / "user-config" / name
        self.project = BASE_DIR / "projects" / name
        self.shots = BASE_DIR / "screenshots" / name
        self.runtime = RUN_HOME / ".cache/blender-virtual" / name / "runtime"

    def prepare(self):
        for directory in (self.state, self.config, self.project, self.runtime):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    def tmux(self, *args, **kwargs):
        return subprocess.run(["/usr/bin/tmux", "-S", str(self.socket)] + list(args), **kwargs)

    def env(self, metadata=None):
        metadata = metadata or read_json(self.metadata) or {}
        threads = str(metadata.get("threads", 8))
        return {
            "HOME": str(RUN_HOME), "USER": RUN_USER, "LOGNAME": RUN_USER,
            "PATH": "/usr/local/bin:/usr/bin:/bin", "SHELL": "/bin/bash", "LANG": "C.UTF-8",
            "DISPLAY": ":{}".format(metadata.get("display_number", 99)),
            "XAUTHORITY": str(self.state / "Xauthority"), "WAYLAND_DISPLAY": "",
            "XDG_RUNTIME_DIR": str(self.runtime), "BLENDER_USER_CONFIG": str(self.config),
            "BLENDER_STATE_DIR": str(self.state), "BLENDER_SESSION_ID": self.name,
            "BLENDER_SESSION_TOKEN": metadata.get("token", ""), "BLENDER_THREADS": threads,
            "LIBGL_ALWAYS_SOFTWARE": "true", "GALLIUM_DRIVER": "llvmpipe",
            "LP_NUM_THREADS": threads, "OMP_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads,
            "CUDA_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": "",
        }

    def status(self):
        metadata = read_json(self.metadata)
        if not metadata:
            return "NOT_STARTED", None
        if alive(metadata.get("supervisor")):
            if metadata["status"] == "running" and all(
                alive(metadata.get(child)) for child in ("blender", "xvfb")
            ):
                return "RUNNING", metadata
            return "STARTING" if metadata["status"] == "starting" else "STOPPING", metadata
        if any(alive(metadata.get(child)) for child in ("blender", "xvfb")):
            return "ORPHANED", metadata
        return "FAILED" if metadata["status"] == "failed" else "STOPPED", metadata

    def ready(self, metadata):
        value = read_json(self.state / "ready.json")
        return value if value and value.get("token") == metadata["token"] else None


def dropped_command(command, environment):
    account = pwd.getpwnam(RUN_USER)
    return ["/usr/bin/setpriv", "--reuid={}".format(account.pw_uid),
            "--regid={}".format(account.pw_gid), "--clear-groups", "--bounding-set=-all",
            "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "/usr/bin/env", "-i"] + [
        "{}={}".format(key, value) for key, value in environment.items()
    ] + command


def allocate():
    directories = STATE_ROOT / "displays"
    directories.mkdir(mode=0o700, exist_ok=True)
    for number in range(99, 1000):
        if Path("/tmp/.X{}-lock".format(number)).exists() or Path("/tmp/.X11-unix/X{}".format(number)).exists():
            continue
        try:
            with lock(directories / "{}.lock".format(number), blocking=False):
                return number
        except BlockingIOError:
            continue
    raise RuntimeError("No free X11 display in :99-:999.")


def used_cpus():
    result = set()
    for path in (STATE_ROOT / "sessions").glob("*/session.json"):
        metadata = read_json(path)
        if metadata and any(alive(metadata.get(child)) for child in ("supervisor", "blender", "xvfb")):
            result.update(metadata.get("cpus", []))
    return result


def send_stop(metadata):
    supervisor = metadata.get("supervisor")
    if not alive(supervisor):
        raise RuntimeError("Supervisor identity no longer matches; no signal sent.")
    os.kill(supervisor["pid"], signal.SIGTERM)


def wait_stopped(session, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, metadata = session.status()
        if state in ("STOPPED", "FAILED", "NOT_STARTED"):
            return
        time.sleep(0.1)
    raise RuntimeError("Stop timed out; inspect {} (no unrelated processes were signalled).".format(session.state))


def wait_ready(session):
    deadline = time.monotonic() + START_TIMEOUT + 5
    while time.monotonic() < deadline:
        state, metadata = session.status()
        if state == "RUNNING" and session.ready(metadata):
            print("Blender session '{}' ready on :{}.".format(session.name, metadata["display_number"]))
            print(json.dumps(session.ready(metadata), indent=2))
            return
        if state in ("FAILED", "STOPPED", "ORPHANED"):
            raise RuntimeError("Startup {}: {}; inspect {}.".format(state.lower(), metadata.get("error", ""), session.state))
        time.sleep(0.2)
    metadata = read_json(session.metadata)
    if alive(metadata.get("supervisor")):
        send_stop(metadata)
        wait_stopped(session)
    raise RuntimeError("Startup timed out; inspect {}.".format(session.state))


def start(session, requested):
    session.prepare()
    with lock(session.state / "operation.lock"):
        state, metadata = session.status()
        if state == "RUNNING":
            print("Session '{}' already running; existing CPU/display settings unchanged.".format(session.name))
            return
        if state == "STARTING":
            wait_ready(session)
            return
        if state in ("ORPHANED", "STOPPING"):
            raise RuntimeError("Session is {}; refusing to overwrite its state.".format(state.lower()))
        legacy = STATE_ROOT / "blender.pid"
        if legacy.exists():
            try:
                if identity(int(legacy.read_text().strip())):
                    raise RuntimeError("Legacy single session still active; stop it before using this manager.")
            except ValueError:
                raise RuntimeError("Invalid legacy PID file; inspect it before starting.")
        with lock(STATE_ROOT / "allocation.lock"):
            used = used_cpus()
            cpus = select_cpus(set(os.sched_getaffinity(0)), used, requested)
            if set(cpus) & used:
                print("Warning: explicit CPU selection overlaps another managed session.", file=sys.stderr)
            metadata = {
                "session": session.name, "token": uuid.uuid4().hex, "status": "starting",
                "display_number": allocate(), "cpus": cpus, "threads": min(8, len(cpus)),
                "started_at": timestamp(), "supervisor": None, "blender": None, "xvfb": None,
            }
            write_json(session.metadata, metadata)
            for filename in ("ready.json", "ready.json.tmp"):
                path = session.state / filename
                if path.exists():
                    path.unlink()
            # The allocation lock stays held until the supervisor acquires its display lock.
            command = "exec /bin/bash {} {}".format(shlex.quote(str(BASE_DIR / "scripts/session.sh")),
                                                    shlex.quote(session.name))
            try:
                session.tmux("-f", "/dev/null", "new-session", "-d", "-s", "blender-virtual", command,
                             env=session.env(metadata), check=True)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    metadata = read_json(session.metadata)
                    if alive(metadata.get("supervisor")):
                        break
                    if metadata["status"] == "failed":
                        raise RuntimeError(metadata.get("error", "Supervisor failed."))
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Supervisor did not acknowledge startup.")
            except (RuntimeError, subprocess.CalledProcessError) as error:
                metadata = read_json(session.metadata)
                if alive(metadata.get("supervisor")):
                    send_stop(metadata)
                    wait_stopped(session)
                else:
                    metadata.update(status="failed", error=str(error), stopped_at=timestamp())
                    write_json(session.metadata, metadata)
                raise
        wait_ready(session)


def stop(session):
    if not session.state.exists():
        print("Session '{}' is not running; no signal sent.".format(session.name))
        return
    with lock(session.state / "operation.lock"):
        state, metadata = session.status()
        if state in ("NOT_STARTED", "STOPPED", "FAILED"):
            print("Session '{}' is not running; no signal sent.".format(session.name))
            return
        if state == "ORPHANED":
            raise RuntimeError("Supervisor missing but child still alive; refusing to signal an unverified target.")
        send_stop(metadata)
        wait_stopped(session)
        print("Session '{}' stopped; its Blender and Xvfb cleaned up.".format(session.name))


def status(session):
    state, metadata = session.status()
    print("{}: {}".format(session.name, state))
    if metadata:
        print(json.dumps(metadata, indent=2))
    if state != "RUNNING":
        return 1
    subprocess.run(["/usr/bin/xdpyinfo"], env=session.env(metadata), stdout=subprocess.DEVNULL, check=True)
    print(json.dumps(session.ready(metadata), indent=2))
    subprocess.run(["/usr/bin/xwininfo", "-root", "-tree"], env=session.env(metadata), check=True)
    return 0


def list_sessions():
    print("SESSION\tSTATE\tDISPLAY\tCPUS")
    for directory in sorted((STATE_ROOT / "sessions").glob("*")):
        if directory.is_dir() and NAME_PATTERN.fullmatch(directory.name):
            state, metadata = Session(directory.name).status()
            print("{}\t{}\t{}\t{}".format(directory.name, state,
                  ":{}".format(metadata["display_number"]) if metadata else "-",
                  cpu_text(metadata["cpus"]) if metadata else "-"))


def screenshot(session):
    if os.geteuid() != 0:
        raise RuntimeError("Publish screenshots from the root terminal.")
    if not session.state.exists():
        raise RuntimeError("Session '{}' has not been started.".format(session.name))
    with lock(session.state / "operation.lock"):
        state, metadata = session.status()
        if state != "RUNNING":
            raise RuntimeError("Session '{}' is not running.".format(session.name))
        session.shots.mkdir(mode=0o755, parents=True, exist_ok=True)
        session.shots.chmod(0o755)
        with tempfile.NamedTemporaryFile(prefix="blender-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-"),
                                         suffix=".png", dir=str(session.shots), delete=False) as output:
            filename = Path(output.name)
            try:
                command = dropped_command(["/usr/bin/import", "-window", "root", "png:-"], session.env(metadata))
                subprocess.run(command, stdout=output, timeout=30, check=True)
                output.flush()
                subprocess.run(["/usr/bin/identify", str(filename)], stdout=subprocess.DEVNULL, check=True)
                filename.chmod(0o644)
            except BaseException:
                filename.unlink()
                raise
        print(filename)


def terminate(process, group=False):
    if process is None:
        return
    if process.poll() is not None and not group:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    except ProcessLookupError:
        process.wait()


def supervise(session):
    if os.geteuid() != pwd.getpwnam(RUN_USER).pw_uid:
        raise RuntimeError("Supervisor must run as the dedicated account.")
    with lock(session.state / "session.lock", blocking=False):
        metadata = read_json(session.metadata)
        display = metadata["display_number"]
        with lock(STATE_ROOT / "displays" / "{}.lock".format(display), blocking=False):
            environment = session.env(metadata)
            xvfb = blender = None
            stopping = [False]
            def handle_stop(signum, frame):
                stopping[0] = True
            signal.signal(signal.SIGTERM, handle_stop)
            signal.signal(signal.SIGHUP, handle_stop)
            signal.signal(signal.SIGINT, handle_stop)
            metadata["supervisor"] = identity(os.getpid())
            write_json(session.metadata, metadata)
            try:
                if Path("/tmp/.X{}-lock".format(display)).exists() or Path("/tmp/.X11-unix/X{}".format(display)).exists():
                    raise RuntimeError("Allocated display became occupied; no existing X server will be changed.")
                authority = session.state / "Xauthority"
                authority.write_bytes(b"")
                authority.chmod(0o600)
                cookie = subprocess.check_output(["/usr/bin/mcookie"], universal_newlines=True).strip()
                subprocess.run(["/usr/bin/xauth", "-f", str(authority), "add", environment["DISPLAY"],
                                "MIT-MAGIC-COOKIE-1", cookie], env=environment, check=True)
                with (session.state / "xvfb.log").open("w") as log:
                    xvfb = subprocess.Popen(["/usr/bin/Xvfb", environment["DISPLAY"], "-screen", "0",
                                             "1600x900x24", "-auth", str(authority), "-nolisten", "tcp"],
                                            env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                metadata["xvfb"] = identity(xvfb.pid)
                write_json(session.metadata, metadata)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not stopping[0]:
                    if xvfb.poll() is not None:
                        raise RuntimeError("Xvfb exited during startup.")
                    if subprocess.run(["/usr/bin/xdpyinfo"], env=environment, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=2).returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Xvfb startup stopped or timed out.")
                with (session.state / "glxinfo.log").open("w") as log:
                    subprocess.run(["/usr/bin/glxinfo", "-B"], env=environment, stdout=log, stderr=log,
                                   timeout=15, check=True)
                if stopping[0]:
                    raise RuntimeError("Startup cancelled.")
                def blender_limits():
                    os.sched_setaffinity(0, metadata["cpus"])
                    os.nice(10)
                with (session.state / "blender.log").open("w") as log:
                    blender = subprocess.Popen([
                        str(BASE_DIR / "blender-4.5.15-linux-x64/blender"),
                        "-noaudio", "--disable-autoexec", "--threads", str(metadata["threads"]),
                        "--gpu-backend", "opengl", "--gpu-compilation-subprocesses", "2",
                        "--window-geometry", "0", "0", "1600", "900",
                        "--python-exit-code", "1", "--python", str(BASE_DIR / "scripts/startup.py"),
                    ], env=environment, cwd=str(session.project), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True, preexec_fn=blender_limits)
                metadata["blender"] = identity(blender.pid)
                write_json(session.metadata, metadata)
                deadline = time.monotonic() + START_TIMEOUT
                while not stopping[0]:
                    if xvfb.poll() is not None:
                        raise RuntimeError("Xvfb exited; stopping this session's Blender.")
                    if blender.poll() is not None:
                        if blender.returncode:
                            raise RuntimeError("Blender exited with code {}.".format(blender.returncode))
                        break
                    if metadata["status"] == "starting":
                        ready = session.ready(metadata)
                        if ready and ready.get("background") is False and ready.get("windows"):
                            metadata["status"] = "running"
                            write_json(session.metadata, metadata)
                        elif time.monotonic() > deadline:
                            raise RuntimeError("Blender GUI did not become ready.")
                    time.sleep(0.2)
                metadata["status"] = "stopped"
            except Exception as error:
                metadata.update(status="failed", error=str(error))
                print(str(error), file=sys.stderr, flush=True)
            finally:
                terminate(blender, group=True)
                terminate(xvfb)
                for filename in ("Xauthority", "ready.json", "ready.json.tmp"):
                    path = session.state / filename
                    if path.exists():
                        path.unlink()
                metadata["stopped_at"] = timestamp()
                write_json(session.metadata, metadata)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--supervise":
        os.umask(0o077)
        supervise(Session(argv[1]))
        return 0
    parser = argparse.ArgumentParser(prog="blenderctl", description="Manage independent Blender + Xvfb sessions.")
    parser.add_argument("action", choices=("start", "list", "status", "screenshot", "stop", "attach"), nargs="?", default="status")
    parser.add_argument("session", nargs="?", default=None, help="session name (default: default)")
    parser.add_argument("--cpus", help="start-only CPU affinity, e.g. 0-7; otherwise allocate 8 unused CPUs")
    args = parser.parse_args(argv)
    if args.action == "list" and args.session is not None:
        parser.error("list does not take a session name")
    if args.cpus is not None and args.action != "start":
        parser.error("--cpus is only valid for start")
    session = Session(args.session or "default")
    account = pwd.getpwnam(RUN_USER)
    if os.geteuid() == 0 and args.action != "screenshot":
        command = dropped_command(["/usr/bin/python3", "-I", str(Path(__file__).resolve())] + argv, session.env())
        os.execv(command[0], command)
    if os.geteuid() not in (0, account.pw_uid):
        raise RuntimeError("Use root or the dedicated {} account.".format(RUN_USER))
    os.umask(0o077)
    if args.action == "start":
        start(session, args.cpus)
    elif args.action == "stop":
        stop(session)
    elif args.action == "status":
        return status(session)
    elif args.action == "list":
        list_sessions()
    elif args.action == "screenshot":
        screenshot(session)
    elif args.action == "attach":
        if session.status()[0] != "RUNNING":
            raise RuntimeError("Session is not running.")
        os.execv("/usr/bin/tmux", ["/usr/bin/tmux", "-S", str(session.socket),
                                  "attach-session", "-t", "=blender-virtual"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("blenderctl: {}".format(error), file=sys.stderr)
        sys.exit(1)
