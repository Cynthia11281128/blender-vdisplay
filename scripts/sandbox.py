"""Whitelist root construction for sandboxed sessions; Python 3.6 and stdlib only.

The host side validates a spec and writes a root-owned record. The record's mount
plan is executed by this same file as pid 1 of the new namespaces, before dropping
to the dedicated account. Paths not in the plan do not exist inside the sandbox.
"""

import json
import os
from pathlib import Path
import subprocess
import sys


SCHEMA_VERSION = 1
SPEC_KEYS = {"schema_version", "read_only", "read_write", "tmpdir"}
SYSTEM_PATHS = ("/etc", "/root", "/proc", "/sys", "/dev", "/run", "/tmp", "/var", "/boot")
ROOT = "/mnt"
# /etc is listed per file: it holds world-readable deployment secrets (e.g. /etc/taiji).
ETC_ENTRIES = ("passwd", "group", "nsswitch.conf", "ld.so.cache", "ld.so.conf", "ld.so.conf.d",
               "fonts", "alternatives", "localtime", "mime.types", "xdg")
USR_LINKS = ("bin", "sbin", "lib", "lib64")
DEVICES = ("null", "zero", "random", "urandom", "full")


class SpecError(ValueError):
    pass


def overlaps(first, second):
    first, second = Path(first), Path(second)
    return first == second or first in second.parents or second in first.parents


def checked_path(value, label, forbidden):
    if not isinstance(value, str) or not value.startswith("/"):
        raise SpecError("{} must be an absolute path: {!r}".format(label, value))
    path = Path(value)
    if not path.exists():
        raise SpecError("{} does not exist: {}".format(label, value))
    if str(path.resolve()) != value.rstrip("/") or value == "/":
        raise SpecError("{} must be a resolved non-root path: {}".format(label, value))
    for item in forbidden:
        if overlaps(path, item):
            raise SpecError("{} overlaps a protected path: {}".format(label, value))
    return value.rstrip("/")


def load_spec(path, account_uid, forbidden):
    forbidden = list(forbidden) + [Path(item) for item in SYSTEM_PATHS]
    with open(str(path)) as source:
        raw = json.load(source)
    if not isinstance(raw, dict) or set(raw) != SPEC_KEYS:
        raise SpecError("Sandbox spec keys must be exactly {}".format(sorted(SPEC_KEYS)))
    if raw["schema_version"] is True or raw["schema_version"] != SCHEMA_VERSION:
        raise SpecError("Unsupported sandbox schema_version")
    for key in ("read_only", "read_write"):
        if not isinstance(raw[key], list):
            raise SpecError("{} must be a list".format(key))
    read_only = [checked_path(item, "read_only", forbidden) for item in raw["read_only"]]
    read_write = [checked_path(item, "read_write", forbidden) for item in raw["read_write"]]
    paths = read_only + read_write
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if overlaps(first, second):
                raise SpecError("Sandbox paths overlap: {} {}".format(first, second))
    for item in read_write:
        if os.stat(item).st_uid != account_uid:
            raise SpecError("read_write path must belong to the dedicated account: {}".format(item))
    tmpdir = checked_path(raw["tmpdir"], "tmpdir", forbidden)
    if os.stat(tmpdir).st_uid != account_uid:
        raise SpecError("tmpdir must belong to the dedicated account: {}".format(tmpdir))
    if not any(tmpdir == item or Path(item) in Path(tmpdir).parents for item in read_write):
        raise SpecError("tmpdir must be inside a read_write path: {}".format(tmpdir))
    return {"schema_version": SCHEMA_VERSION, "read_only": read_only,
            "read_write": read_write, "tmpdir": tmpdir}


def mount_plan(spec, program, scripts, writable, home, displays, account):
    """Ordered root contents. Parents of a target always come from an earlier tmpfs."""
    uid, gid = account
    owned = "mode=700,uid={},gid={}".format(uid, gid)
    plan = [{"kind": "tmpfs", "target": "/tmp", "options": "mode=1777,nosuid,nodev"},
            {"kind": "dir", "target": "/tmp/.X11-unix", "mode": 0o1777},
            {"kind": "tmpfs", "target": "/run", "options": "mode=755,nosuid,nodev"},
            {"kind": "tmpfs", "target": str(home), "options": owned + ",nosuid,nodev"},
            {"kind": "tmpfs", "target": str(displays), "options": owned + ",nosuid,nodev"},
            {"kind": "bind", "source": "/usr", "target": "/usr", "read_only": True}]
    plan += [{"kind": "symlink", "source": "usr/" + name, "target": "/" + name} for name in USR_LINKS]
    for name in ETC_ENTRIES:
        plan.append({"kind": "bind", "source": "/etc/" + name, "target": "/etc/" + name,
                     "read_only": True, "optional": True})
    plan += [{"kind": "proc", "target": "/proc"},
             {"kind": "tmpfs", "target": "/dev", "options": "mode=755,nosuid"},
             {"kind": "tmpfs", "target": "/dev/shm", "options": "mode=1777,nosuid,nodev"}]
    for name in DEVICES:
        plan.append({"kind": "bind", "source": "/dev/" + name, "target": "/dev/" + name,
                     "read_only": False, "device": True})
    for path in (str(program), str(scripts)):
        plan.append({"kind": "bind", "source": path, "target": path, "read_only": True})
    for path in writable:
        plan.append({"kind": "bind", "source": str(path), "target": str(path), "read_only": False})
    for path in spec["read_only"]:
        plan.append({"kind": "bind", "source": path, "target": path, "read_only": True})
    for path in spec["read_write"]:
        plan.append({"kind": "bind", "source": path, "target": path, "read_only": False})
    return plan


def run(*command):
    subprocess.run([str(item) for item in command], check=True)


def inside(target):
    return ROOT + target


def make_parents(path, home, account):
    """Create missing parents; those below the private home belong to the account."""
    pending = []
    current = Path(path).parent
    while not os.path.lexists(str(current)):
        pending.append(current)
        current = current.parent
    for directory in reversed(pending):
        os.mkdir(str(directory), 0o755)
        if Path(inside(home)) in directory.parents:
            os.chown(str(directory), *account)


def apply_entry(entry, home, account):
    kind = entry["kind"]
    target = inside(entry["target"])
    if kind == "bind" and entry.get("optional") and not os.path.lexists(entry["source"]):
        return
    make_parents(target, home, account)
    if kind == "symlink":
        os.symlink(entry["source"], target)
        return
    if kind == "dir":
        os.mkdir(target)
        os.chmod(target, entry["mode"])
        return
    source_is_directory = kind != "bind" or os.path.isdir(entry["source"])
    if source_is_directory:
        os.makedirs(target, exist_ok=True)
    elif not os.path.exists(target):
        open(target, "a").close()
    if kind == "tmpfs":
        run("/usr/bin/mount", "-t", "tmpfs", "-o", entry["options"], "tmpfs", target)
    elif kind == "proc":
        run("/usr/bin/mount", "-t", "proc", "-o", "nosuid,nodev,noexec", "proc", target)
    else:
        run("/usr/bin/mount", "--bind", entry["source"], target)
        flags = "remount,bind" + (",nosuid" if not entry.get("device") else ",nosuid,noexec")
        if entry["read_only"]:
            flags += ",ro"
        elif not entry.get("device"):
            flags += ",nodev"
        run("/usr/bin/mount", "-o", flags, target)


def build_root(record):
    home, account = record["home"], tuple(record["account"])
    run("/usr/bin/mount", "--make-rprivate", "/")
    run("/usr/bin/mount", "-t", "tmpfs", "-o", "mode=755,nosuid,nodev", "tmpfs", ROOT)
    for entry in record["plan"]:
        apply_entry(entry, home, account)
    old = inside("/.old-root")
    os.mkdir(old, 0o700)
    os.chdir(ROOT)
    run("/usr/sbin/pivot_root", ".", ".old-root")
    os.chdir("/")
    run("/usr/bin/umount", "-l", "/.old-root")
    os.rmdir("/.old-root")


def initialize(record_path):
    """Pid 1 of the namespaces. Any failure ends the namespace, never a weaker start."""
    with open(record_path) as source:
        record = json.load(source)
    if os.getpid() != 1:
        raise RuntimeError("Sandbox init must be pid 1 of a new pid namespace.")
    # The caller's umask (077) would make the new root's directories untraversable.
    os.umask(0o022)
    build_root(record)
    command = record["command"]
    os.execv(command[0], command)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--init":
        sys.exit("usage: sandbox.py --init RECORD")
    try:
        initialize(sys.argv[2])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("sandbox init: {}".format(error), file=sys.stderr, flush=True)
        sys.exit(1)
