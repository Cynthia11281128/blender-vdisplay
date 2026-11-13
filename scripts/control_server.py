"""Socket threads handle transport only; tick() executes on Blender's main thread."""

import atexit
import contextlib
import hashlib
import json
import os
import select
import socket
import struct
import sys
import threading
import time
import traceback

import control_protocol as protocol


class OutputBudget:
    def __init__(self):
        self.remaining = protocol.MAX_OUTPUT
        self.exceeded = False


class CapturedOutput:
    encoding = "utf-8"

    def __init__(self, budget):
        self.budget = budget
        self.chunks = []

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("Output must be text.")
        # Slice before encoding so a single huge write cannot allocate unbounded bytes.
        encoded = text[:self.budget.remaining + 1].encode("utf-8", errors="replace")
        if len(encoded) > self.budget.remaining or len(text) > len(encoded):
            self.budget.exceeded = True
        kept = encoded[:self.budget.remaining].decode("utf-8", errors="ignore")
        self.budget.remaining -= len(kept.encode("utf-8"))
        if kept:
            self.chunks.append(kept)
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.chunks)


def exception_details(error):
    try:
        message = str(error)[:8192]
    except BaseException:
        message = "<exception message unavailable>"
    output = CapturedOutput(OutputBudget())
    output.budget.remaining = 65536
    output.write("Traceback (most recent call last):\n")
    for frame in traceback.extract_tb(error.__traceback__, limit=32):
        output.write('  File "{}", line {}, in {}\n'.format(
            frame.filename[:4096], frame.lineno, frame.name[:4096]))
        if frame.line:
            output.write("    " + frame.line[:8192] + "\n")
    output.write(type(error).__name__ + ": " + message + "\n")
    return {"code": "execution_error", "message": message,
            "type": type(error).__name__, "traceback": output.getvalue()}


def execute_request(request, generation, namespace):
    value = protocol.response(request, generation)
    budget = OutputBudget()
    stdout, stderr = CapturedOutput(budget), CapturedOutput(budget)
    started = time.monotonic()
    namespace.pop("result", None)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(request["code"], request["filename"], "exec"), namespace, namespace)
        value["result"] = namespace.get("result")
    except BaseException as error:
        value.update(ok=False, error=exception_details(error))
    value.update(stdout=stdout.getvalue(), stderr=stderr.getvalue(),
                 duration_ns=int((time.monotonic() - started) * 1000000000))
    if budget.exceeded:
        value.update(ok=False, result=None,
                     error={"code": "response_too_large", "message": "Captured output exceeds shared byte budget; side effects are not rolled back."})
    try:
        frame = protocol.encode_frame(value, protocol.MAX_RESPONSE)
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        code = "response_too_large" if isinstance(error, protocol.ProtocolError) else "result_not_serializable"
        value.update(ok=False, result=None, error={"code": code, "message": str(error)[:8192]})
        try:
            frame = protocol.encode_frame(value, protocol.MAX_RESPONSE)
        except protocol.ProtocolError:
            # Escaped captured text alone can exceed the response frame budget.
            value.update(stdout="", stderr="", error={
                "code": "response_too_large",
                "message": "Encoded output exceeds response frame limit; output omitted, side effects are not rolled back.",
            })
            frame = protocol.encode_frame(value, protocol.MAX_RESPONSE)
    return frame, value


class ControlServer:
    def __init__(self, path, generation, bpy_module):
        self.path = protocol.socket_path(path)
        self.generation = generation
        self.bpy = bpy_module
        self.namespace = {"bpy": bpy_module, "__name__": "__blenderctl__"}
        self.lock = threading.Lock()
        self.slot = None
        self.closed = threading.Event()
        self.connections = set()
        self.workers = set()
        self.capacity = threading.BoundedSemaphore(8)
        self.log_stream = sys.__stderr__
        self.listener = None
        self.inode = None

    def log(self, event, request, **fields):
        fields.update(event=event, request_id=request.get("request_id"),
                      generation=hashlib.sha256(self.generation.encode("utf-8")).hexdigest()[:12])
        print("BLENDER_CONTROL " + json.dumps(fields), file=self.log_stream, flush=True)

    def start(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(self.path)
            self.inode = os.stat(self.path).st_ino
            os.chmod(self.path, 0o600)
            self.listener.listen(8)
            self.listener.settimeout(0.2)
            self.bpy.app.timers.register(self.tick, first_interval=0.05, persistent=True)
            self.accept_thread = threading.Thread(target=self.accept, name="blender-control", daemon=True)
            self.accept_thread.start()
            atexit.register(self.close)
        except BaseException:
            self.close()
            raise
        return self

    def accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            if uid not in (0, os.getuid()) or not self.capacity.acquire(False):
                connection.close()
                continue
            worker = threading.Thread(target=self.handle, args=(connection,), daemon=True)
            with self.lock:
                self.connections.add(connection)
                self.workers.add(worker)
            worker.start()

    def handle(self, connection):
        item = None
        request = {}
        try:
            try:
                request = protocol.receive_frame(connection, protocol.MAX_REQUEST, time.monotonic() + 5)
                protocol.validate_request(request)
            except (ValueError, TimeoutError, OSError) as error:
                self.send_error(connection, request, "invalid_request", str(error))
                return
            if request["generation"] != self.generation:
                self.send_error(connection, request, "generation_mismatch", "Session was restarted; query status again.")
                return
            with self.lock:
                if self.slot is None and not self.closed.is_set():
                    item = {"request": request, "started": False, "cancelled": False,
                            "done": threading.Event(), "frame": None}
                    self.slot = item
            if item is None:
                self.send_error(connection, request, "busy", "An execution request is already admitted.")
                return
            while not item["done"].wait(0.05):
                if self.closed.is_set():
                    return
                if select.select([connection], [], [], 0)[0]:
                    # Any extra client bytes violate the one-request connection contract.
                    return
            connection.settimeout(5)
            connection.sendall(item["frame"])
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                if item is not None and not item["started"]:
                    item["cancelled"] = True
                    if self.slot is item:
                        self.slot = None
                self.connections.discard(connection)
                self.workers.discard(threading.current_thread())
            connection.close()
            self.capacity.release()

    def send_error(self, connection, request, code, message):
        value = protocol.response(request, self.generation, {"code": code, "message": message[:8192]})
        connection.settimeout(5)
        connection.sendall(protocol.encode_frame(value, protocol.MAX_RESPONSE))

    def tick(self):
        if self.closed.is_set():
            return None
        with self.lock:
            item = self.slot
            if item is None or item["started"] or item["cancelled"]:
                return 0.05
            item["started"] = True
        self.log("start", item["request"])
        try:
            frame, value = execute_request(item["request"], self.generation, self.namespace)
            item["frame"] = frame
            self.log("end", item["request"], duration_ns=value["duration_ns"],
                     error=value.get("error", {}).get("code"))
        finally:
            with self.lock:
                if self.slot is item:
                    self.slot = None
            item["done"].set()
        return 0.05

    def close(self):
        self.closed.set()
        if self.listener is not None:
            self.listener.close()
        with self.lock:
            connections = list(self.connections)
            threads = list(self.workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            if self.inode is not None and os.stat(self.path).st_ino == self.inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        if hasattr(self, "accept_thread"):
            threads.append(self.accept_thread)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)


class PollingControlServer(ControlServer):
    """Nonblocking transport and execution on the GUI timer; no Python threads."""

    def __init__(self, path, generation, bpy_module):
        super().__init__(path, generation, bpy_module)
        self.clients = {}

    def start(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(self.path)
            self.inode = os.stat(self.path).st_ino
            os.chmod(self.path, 0o600)
            self.listener.listen(8)
            self.listener.setblocking(False)
            self.bpy.app.timers.register(self.tick, first_interval=0.05, persistent=True)
            atexit.register(self.close)
        except BaseException:
            self.close()
            raise
        return self

    def drop(self, connection):
        self.clients.pop(connection, None)
        if self.slot and self.slot["connection"] is connection:
            self.slot = None
        connection.close()

    def reply(self, item, request, code, message):
        item.update(frame=protocol.encode_frame(
            protocol.response(request, self.generation, {"code": code, "message": message}),
            protocol.MAX_RESPONSE), offset=0, deadline=time.monotonic() + 5)

    def accept_ready(self):
        for _ in range(8):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                break
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            if uid not in (0, os.getuid()) or len(self.clients) >= 8:
                connection.close()
                continue
            connection.setblocking(False)
            self.clients[connection] = {"connection": connection, "data": bytearray(), "frame": None,
                                        "request": None, "deadline": time.monotonic() + 5,
                                        "busy": self.slot is not None}

    def receive_ready(self):
        for connection, item in list(self.clients.items()):
            if item["frame"] is not None or item["request"] is not None:
                continue
            try:
                chunk = connection.recv(min(65536, protocol.MAX_REQUEST + 1 - len(item["data"])))
            except BlockingIOError:
                continue
            except OSError:
                self.drop(connection)
                continue
            if not chunk:
                self.drop(connection)
                continue
            item["data"].extend(chunk)
            data = item["data"]
            try:
                if len(data) > protocol.MAX_REQUEST:
                    raise ValueError("JSON frame exceeds byte limit.")
                if b"\n" not in data:
                    continue
                if not data.endswith(b"\n") or data.count(b"\n") != 1:
                    raise ValueError("Expected exactly one JSON line.")
                request = json.loads(data.decode("utf-8"), parse_constant=protocol.reject_constant,
                                     parse_float=protocol.finite_float)
                protocol.validate_request(request)
                if request["generation"] != self.generation:
                    self.reply(item, request, "generation_mismatch", "Session was restarted; query status again.")
                elif item["busy"] or self.slot is not None:
                    self.reply(item, request, "busy", "An execution request is already admitted.")
                else:
                    item["request"] = request
                    self.slot = item
            except (ValueError, UnicodeError, RecursionError) as error:
                self.reply(item, {}, "invalid_request", str(error)[:8192])

    def send_ready(self):
        for connection, item in list(self.clients.items()):
            if item["frame"] is None:
                if item is not self.slot and time.monotonic() > item["deadline"]:
                    self.drop(connection)
                continue
            try:
                count = connection.send(item["frame"][item["offset"]:])
                item["offset"] += count
                if not count or item["offset"] == len(item["frame"]):
                    self.drop(connection)
            except BlockingIOError:
                if time.monotonic() > item["deadline"]:
                    self.drop(connection)
            except OSError:
                self.drop(connection)

    def tick(self):
        if self.closed.is_set():
            return None
        self.accept_ready()
        self.receive_ready()
        item = self.slot
        if item is not None and item["frame"] is None:
            connection = item["connection"]
            # Cancel disconnected pending work, but never claim that a started
            # task was cancelled if its client times out during execution.
            if select.select([connection], [], [], 0)[0]:
                self.drop(connection)
            else:
                self.log("start", item["request"])
                frame, value = execute_request(item["request"], self.generation, self.namespace)
                item.update(frame=frame, offset=0, deadline=time.monotonic() + 5)
                self.log("end", item["request"], duration_ns=value["duration_ns"],
                         error=value.get("error", {}).get("code"))
                # Connections queued during synchronous execution are busy,
                # even if their frame arrives only after execution completes.
                self.accept_ready()
                self.receive_ready()
        self.send_ready()
        return 0.05

    def close(self):
        self.closed.set()
        if self.listener is not None:
            self.listener.close()
        for connection in list(self.clients):
            self.drop(connection)
        try:
            if self.inode is not None and os.stat(self.path).st_ino == self.inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass
