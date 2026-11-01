"""Bounded, dependency-free local control protocol (Python 3.6 compatible)."""

import json
import math
import os
import time


PROTOCOL_VERSION = 1
MAX_CODE = 1024 * 1024
MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


class ProtocolError(ValueError):
    pass


def positive_timeout(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be a finite positive number.")
    return value


def socket_path(value):
    value = os.fspath(value)
    if not os.path.isabs(value) or "\0" in value or len(os.fsencode(value)) >= 108:
        raise ValueError("Unix socket path must be absolute and shorter than 108 bytes.")
    return value


def encode_frame(value, limit):
    # iterencode can allocate an entire scalar string in one chunk. Preflight
    # bounds both those scalars and traversal work before invoking the encoder.
    def check(item, depth, budget, seen):
        if depth > 128:
            raise ValueError("JSON nesting exceeds 128 levels.")
        budget[0] -= 1
        if budget[0] < 0:
            raise ProtocolError("JSON value exceeds byte limit.")
        if isinstance(item, str):
            budget[0] -= len(item)
            if budget[0] < 0:
                raise ProtocolError("JSON value exceeds byte limit.")
        elif isinstance(item, int) and item.bit_length() > limit * 3:
            raise ProtocolError("JSON integer exceeds byte limit.")
        elif isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in seen:
                raise ValueError("Circular JSON value.")
            seen.add(identity)
            if isinstance(item, dict):
                for key, child in item.items():
                    check(key, depth + 1, budget, seen)
                    check(child, depth + 1, budget, seen)
            else:
                for child in item:
                    check(child, depth + 1, budget, seen)
            seen.remove(identity)
    check(value, 0, [limit], set())
    chunks = []
    size = 1
    for text in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
        chunk = text.encode("utf-8")
        size += len(chunk)
        if size > limit:
            raise ProtocolError("JSON frame exceeds byte limit.")
        chunks.append(chunk)
    return b"".join(chunks) + b"\n"


def receive_frame(connection, limit, deadline):
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Frame deadline expired.")
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, limit + 1 - len(data)))
        if not chunk:
            raise ProtocolError("Connection closed before a complete frame.")
        data.extend(chunk)
        if len(data) > limit:
            raise ProtocolError("JSON frame exceeds byte limit.")
        if b"\n" in chunk:
            if not data.endswith(b"\n") or data.count(b"\n") != 1:
                raise ProtocolError("Expected exactly one JSON line.")
            try:
                return json.loads(data.decode("utf-8"), parse_constant=reject_constant,
                                  parse_float=finite_float)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ProtocolError("Invalid UTF-8 JSON: {}".format(error))
        if len(data) == limit:
            raise ProtocolError("JSON frame exceeds byte limit.")


def reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number.")
    return number


def validate_request(value):
    if not isinstance(value, dict):
        raise ProtocolError("Request must be an object.")
    if type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("Unsupported protocol version.")
    for key in ("request_id", "generation", "filename"):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key].encode("utf-8")) > 4096:
            raise ProtocolError("Invalid " + key + ".")
    if value.get("operation") != "exec":
        raise ProtocolError("Unsupported operation.")
    if not isinstance(value.get("code"), str) or len(value["code"].encode("utf-8")) > MAX_CODE:
        raise ProtocolError("Code exceeds byte limit or is not text.")
    return value


def response(request, generation, error=None):
    value = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.get("request_id") if isinstance(request, dict) else None,
        "generation": generation, "ok": error is None,
        "stdout": "", "stderr": "", "result": None, "duration_ns": 0,
    }
    if error is not None:
        value["error"] = error
    return value


def validate_response(value, request):
    if not isinstance(value, dict):
        raise ProtocolError("Response must be an object.")
    if type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("Unsupported response version.")
    if value.get("request_id") != request["request_id"] or value.get("generation") != request["generation"]:
        raise ProtocolError("Response request ID or generation mismatch.")
    if type(value.get("ok")) is not bool:
        raise ProtocolError("Invalid response status.")
    for key in ("stdout", "stderr"):
        if not isinstance(value.get(key), str):
            raise ProtocolError("Invalid response output.")
    if type(value.get("duration_ns")) is not int or value["duration_ns"] < 0 or "result" not in value:
        raise ProtocolError("Invalid response result or duration.")
    if not value["ok"] and (
        not isinstance(value.get("error"), dict) or
        not isinstance(value["error"].get("code"), str) or
        not isinstance(value["error"].get("message"), str)
    ):
        raise ProtocolError("Invalid response error.")
    return value
