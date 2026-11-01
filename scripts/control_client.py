"""Small trusted-local client; never retries requests."""

import socket
import time
import uuid

import control_protocol as protocol


class ControlClientError(RuntimeError):
    pass


def execute(socket_path, generation, code, timeout=30, filename="<blenderctl>"):
    timeout = protocol.positive_timeout(timeout)
    path = protocol.socket_path(socket_path)
    request = protocol.validate_request({
        "protocol_version": protocol.PROTOCOL_VERSION, "request_id": uuid.uuid4().hex,
        "generation": generation, "operation": "exec", "filename": filename, "code": code,
    })
    frame = protocol.encode_frame(request, protocol.MAX_REQUEST)
    deadline = time.monotonic() + timeout
    submitted = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(path)
            connection.settimeout(max(0.000001, deadline - time.monotonic()))
            # A failed send may have delivered the entire request.
            submitted = True
            connection.sendall(frame)
            value = protocol.receive_frame(connection, protocol.MAX_RESPONSE, deadline)
            return protocol.validate_response(value, request)
    except (OSError, TimeoutError, protocol.ProtocolError) as error:
        message = "Control transport failed: {}".format(error)
        if submitted:
            message += "; result unknown, execution may still be running or have completed. Do not automatically retry."
        raise ControlClientError(message)
