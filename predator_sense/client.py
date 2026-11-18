"""Client for the PredatorSense service socket."""

import json
import socket

from .daemon import NOT_ALLOWED, SOCKET_PATH


class ServiceError(Exception):
    pass


def request(cmd, socket_path=SOCKET_PATH, timeout=3.0, **params):
    message = dict(params, cmd=cmd)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(json.dumps(message).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        raise ServiceError("The PredatorSense service is not running. Start it with: sudo systemctl start predator-sense")
    except PermissionError:
        raise ServiceError(NOT_ALLOWED)
    except OSError as e:
        raise ServiceError(f"Cannot talk to the PredatorSense service: {e}")
    try:
        response = json.loads(buf)
    except json.JSONDecodeError:
        raise ServiceError("Invalid response from the PredatorSense service")
    if not response.get("ok"):
        raise ServiceError(response.get("error", "Unknown error"))
    return response.get("data")
