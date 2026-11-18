"""Root service: the only process that touches the hardware.

The GUI and CLI talk to it over a Unix socket using one JSON object per line.
"""

import grp
import json
import logging
import os
import pwd
import signal
import socket
import socketserver
import struct
import threading
import time

from . import hardware, rgb
from .ec import EC, ECError

SOCKET_PATH = "/run/predator-sense/socket"
STATE_DIR = "/var/lib/predator-sense"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
VERIFIED_FILE = os.path.join(STATE_DIR, "verified.json")
GROUP = "predatorsense"
SAFETY_TEMP = 95
SAFETY_INTERVAL = 2.0
# Lowest custom GPU fan speed allowed while the GPU temperature cannot be monitored.
BLIND_MIN_SPEED = 40
RESUME_GAP = 1.0
MAX_REQUEST = 4096
MAX_CONNECTIONS = 16
CONNECTION_TIMEOUT = 10.0

INSTALL_HINT = "Fan control needs the PredatorSense build of the facer module. Re-run: sudo ./install.sh"
VERIFY_HINT = "Run: sudo predator-sense verify"
NOT_ALLOWED = f"Not allowed. Add your user to the '{GROUP}' group, then log out and back in."

log = logging.getLogger("predator-sense")

DEFAULT_STATE = {"fans": {"cpu": "auto", "gpu": "auto"}, "rgb": None, "kb_timeout": None}


def suspend_offset():
    """Seconds spent suspended since boot (CLOCK_BOOTTIME counts suspend, CLOCK_MONOTONIC does not)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def verification_problem(path=VERIFIED_FILE):
    """None if the saved hardware check still applies to this machine, else why fan control is locked."""
    try:
        with open(path) as f:
            saved = json.load(f)
    except FileNotFoundError:
        return f"Fan control is locked until the hardware check passes. {VERIFY_HINT}"
    except (OSError, ValueError):
        saved = None
    if not isinstance(saved, dict):
        return f"The saved hardware check is unreadable, so fan control is locked. {VERIFY_HINT}"
    names = {"model": "laptop model", "bios": "BIOS", "facer": "facer module"}
    changed = [names[key] for key, value in hardware.fingerprint().items() if saved.get(key) != value]
    if changed:
        return f"The {' and '.join(changed)} changed since the hardware check, so fan control is locked. {VERIFY_HINT}"
    return None


def is_authorized(uid, group=GROUP):
    if uid == 0:
        return True
    try:
        user = pwd.getpwuid(uid)
        g = grp.getgrnam(group)
    except KeyError:
        return False
    return user.pw_gid == g.gr_gid or user.pw_name in g.gr_mem


def peer_uid(sock):
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class Service:
    def __init__(self, controller, keyboard, state_file=STATE_FILE, verified_file=VERIFIED_FILE):
        self.controller = controller
        self.keyboard = keyboard
        self.state_file = state_file
        self.verified_file = verified_file
        self.lock = threading.Lock()
        self.notice = None
        self.state = self._load_state()
        self._fans_pending = False
        self._rgb_pending = False
        self._kb_timeout_pending = False
        self._suspend_offset = suspend_offset()

    # --- persistence -------------------------------------------------------

    def _load_state(self):
        state = json.loads(json.dumps(DEFAULT_STATE))
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
            for fan in hardware.FANS:
                state["fans"][fan] = hardware.parse_fan_setting(saved["fans"][fan])
            if saved.get("rgb") is not None:
                state["rgb"] = rgb.normalize_settings(saved["rgb"])
            kb_timeout = saved.get("kb_timeout")
            if kb_timeout is not None:
                if not isinstance(kb_timeout, bool):
                    raise ValueError("kb_timeout must be true or false")
                state["kb_timeout"] = kb_timeout
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Ignoring unreadable state file: %s", e)
        return state

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("Could not save settings: %s", e)

    def fan_control_problem(self):
        """None if fans may be controlled, else a message saying why not."""
        if not self.controller.can_control_fans:
            return INSTALL_HINT
        return verification_problem(self.verified_file)

    @property
    def fan_control_enabled(self):
        return self.fan_control_problem() is None

    def _gpu_blind(self, setting):
        """True if `setting` is a low custom GPU speed while the GPU temperature cannot be read."""
        return (isinstance(setting, int) and setting < BLIND_MIN_SPEED
                and self.controller.gpu_temp() is None)

    # --- startup / shutdown ------------------------------------------------

    def restore(self):
        with self.lock:
            self._restore_fans()
            self._restore_rgb()
            self._restore_kb_timeout()

    def _restore_fans(self):
        fans = dict(self.state["fans"])
        # The facer module can appear after the service starts; tick() retries then.
        self._fans_pending = (not self.controller.can_control_fans
                              and any(s != "auto" for s in fans.values()))
        if not self.fan_control_enabled:
            return
        if self._gpu_blind(fans["gpu"]):
            log.warning("GPU temperature unavailable; restoring the GPU fan at %d%%", BLIND_MIN_SPEED)
            fans["gpu"] = BLIND_MIN_SPEED
            self.state["fans"] = fans
            self._save_state()
        try:
            self.controller.set_both(fans["cpu"], fans["gpu"])
        except (OSError, ValueError) as e:
            log.error("Could not restore fan settings: %s", e)

    def _restore_rgb(self):
        self._rgb_pending = bool(self.state["rgb"]) and not self.keyboard.available()
        if not self.state["rgb"] or self._rgb_pending:
            return
        try:
            self.keyboard.apply(self.state["rgb"])
        except OSError as e:
            log.error("Could not restore keyboard lighting: %s", e)

    def _restore_kb_timeout(self):
        on = self.state["kb_timeout"]
        self._kb_timeout_pending = on is not None and not self.keyboard.timeout_available()
        if on is None or self._kb_timeout_pending:
            return
        try:
            self.keyboard.set_timeout(on)
        except (OSError, ValueError) as e:
            log.error("Could not restore the keyboard backlight timeout: %s", e)

    def tick(self):
        """Periodic work: re-apply after resume, finish a delayed restore, then the safety check."""
        offset = suspend_offset()
        resumed = offset - self._suspend_offset > RESUME_GAP
        self._suspend_offset = offset
        with self.lock:
            if resumed:
                log.info("Resumed from suspend; re-applying fan and keyboard settings")
                self._restore_fans()
                self._restore_rgb()
                self._restore_kb_timeout()
            else:
                if self._fans_pending and self.controller.can_control_fans:
                    self._restore_fans()
                if self._rgb_pending and self.keyboard.available():
                    self._restore_rgb()
                if self._kb_timeout_pending and self.keyboard.timeout_available():
                    self._restore_kb_timeout()
        self.safety_check()

    def shutdown(self):
        with self.lock:
            if self.fan_control_enabled:
                for err in self.controller.reset():
                    log.error("Could not reset fan to auto: %s", err)

    def safety_check(self):
        """Give fans back to the firmware if it gets too hot in manual mode."""
        with self.lock:
            if not self.fan_control_enabled:
                return
            if not any(isinstance(s, int) for s in self.state["fans"].values()):
                return
            try:
                s = self.controller.status()
            except OSError as e:
                log.warning("Safety check could not read sensors: %s", e)
                return
            manual = [f for f in hardware.FANS if s[f + "_fan"]["mode"] == "manual"]
            hottest = max(t for t in (s["cpu_temp"], s["gpu_temp"], 0) if t is not None)
            if manual and hottest >= SAFETY_TEMP:
                self._fail_safe()
                self.notice = f"Temperature reached {hottest}°C, so fans were switched back to Auto."
                log.warning(self.notice)

    # --- requests ----------------------------------------------------------

    def handle(self, request):
        try:
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            cmd = request.get("cmd")
            if not isinstance(cmd, str):
                raise ValueError("cmd must be a string")
            handler = {
                "status": self._status,
                "set_fans": self._set_fans,
                "set_turbo": self._set_turbo,
                "set_rgb": self._set_rgb,
                "set_kb_timeout": self._set_kb_timeout,
            }.get(cmd)
            if handler is None:
                raise ValueError(f"Unknown command: {cmd!r}")
            with self.lock:
                return {"ok": True, "data": handler(request)}
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def _status(self, _request):
        problem = self.fan_control_problem()
        data = {
            "model": hardware.detect_model(),
            "fan_control": problem is None,
            "fan_control_problem": problem,
            "keyboard": self.keyboard.available(),
            "rgb": self.state["rgb"] or dict(rgb.DEFAULT_SETTINGS),
            # Unset means the firmware default, which is the 30 second timeout.
            "kb_timeout": self.state["kb_timeout"] is not False,
            "kb_timeout_control": self.keyboard.timeout_available(),
            "saved_fans": self.state["fans"],
            "notice": self.notice,
            "sensors": None,
            "error": None,
        }
        try:
            sensors = self.controller.status()
        except OSError as e:
            data["error"] = f"Cannot read sensors: {e}"
            return data
        data["sensors"] = sensors
        if sensors["cpu_rpm"] is None and sensors["gpu_rpm"] is None:
            data["error"] = ("Fan speeds cannot be read: the firmware does not report them "
                             "and the embedded controller (EC) is not accessible.")
        return data

    def _require_fan_control(self):
        problem = self.fan_control_problem()
        if problem:
            raise OSError(problem)

    def _fail_safe(self):
        for err in self.controller.reset():
            log.error("Could not reset fan to auto: %s", err)
        self.state["fans"] = {fan: "auto" for fan in hardware.FANS}
        self._save_state()

    def _apply_fans(self, fans):
        try:
            self.controller.set_both(fans["cpu"], fans["gpu"])
        except OSError:
            self._fail_safe()
            raise
        self.state["fans"] = fans
        self.notice = None
        self._save_state()

    def _set_fans(self, request):
        self._require_fan_control()
        given = [fan for fan in hardware.FANS if request.get(fan) is not None]
        if not given:
            raise ValueError("Give a setting for cpu and/or gpu: auto, max or 0-100")
        fans = dict(self.state["fans"])
        for fan in given:
            fans[fan] = hardware.parse_fan_setting(request[fan])
        if "gpu" in given and self._gpu_blind(fans["gpu"]):
            raise ValueError(f"The GPU temperature cannot be read, so the GPU fan cannot be set "
                             f"below {BLIND_MIN_SPEED}%.")
        self._apply_fans(fans)
        return None

    def _set_turbo(self, request):
        self._require_fan_control()
        on = request.get("on")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        self._apply_fans({fan: "max" if on else "auto" for fan in hardware.FANS})
        return None

    def _set_rgb(self, request):
        settings = rgb.normalize_settings(request.get("settings"), base=self.state["rgb"])
        self.keyboard.apply(settings)
        self.state["rgb"] = settings
        self._save_state()
        return settings

    def _set_kb_timeout(self, request):
        on = request.get("on")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        self.keyboard.set_timeout(on)
        self.state["kb_timeout"] = on
        self._kb_timeout_pending = False
        self._save_state()
        return None


class _Handler(socketserver.StreamRequestHandler):
    timeout = CONNECTION_TIMEOUT

    def handle(self):
        try:
            uid = peer_uid(self.connection)
        except OSError:
            return
        if not self.server.authorize(uid):
            # Read the request first so the client is not cut off before it gets the reason.
            self._read_line()
            self._reply({"ok": False, "error": NOT_ALLOWED})
            return
        if not self.server.slots.acquire(blocking=False):
            self._read_line()
            self._reply({"ok": False, "error": "The PredatorSense service is busy; try again."})
            return
        try:
            self._serve()
        finally:
            self.server.slots.release()

    def _read_line(self):
        try:
            return self.rfile.readline(MAX_REQUEST + 1)
        except OSError:  # includes the idle timeout
            return b""

    def _serve(self):
        while True:
            line = self._read_line()
            if not line:
                return
            if len(line) > MAX_REQUEST:
                self._reply({"ok": False, "error": "Request too large"})
                return
            try:
                response = self.server.service.handle(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = {"ok": False, "error": "Invalid JSON"}
            except Exception:  # a bad request must never take down the connection handler silently
                log.exception("Unexpected error handling a request")
                response = {"ok": False, "error": "Internal error in the PredatorSense service"}
            self._reply(response)

    def _reply(self, response):
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, service, authorize=is_authorized, group=GROUP):
        self.service = service
        self.authorize = authorize
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        if os.path.lexists(path):
            os.unlink(path)
        old_umask = os.umask(0o177)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old_umask)
        # Only root and the group may connect; each request is also authorized by the caller's uid.
        try:
            gid = grp.getgrnam(group).gr_gid if group else None
        except KeyError:
            gid = None
            log.warning("Group %r does not exist; only root can use the service", group)
        if gid is not None:
            os.chown(path, -1, gid)
            os.chmod(path, 0o660)


def _make_controller():
    try:
        ec = EC()
    except ECError as e:
        log.warning("%s Using firmware sensors only.", e)
        ec = None
    return hardware.Controller(ec)


def reset_fans():
    """Used by systemd ExecStopPost so fans return to Auto even after a crash."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    fans = hardware.WmiFans()
    if not fans.available():
        return 0
    try:
        fans.set("auto", "auto")
    except OSError as e:
        log.error("Could not reset fans to auto: %s", e)
        return 1
    return 0


def run(socket_path=SOCKET_PATH):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        log.error("The service must run as root.")
        return 1
    model = hardware.detect_model()
    if not hardware.is_supported_model(model):
        log.error("Unsupported model %r; supported: %s", model, ", ".join(hardware.SUPPORTED_MODELS))
        return 1

    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    service = Service(_make_controller(), rgb.Keyboard())
    service.restore()
    server = Server(socket_path, service)

    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def watchdog():
        while not stop.wait(SAFETY_INTERVAL):
            try:
                service.tick()
            except Exception:  # the safety check must keep running whatever happens
                log.exception("Watchdog error")

    threading.Thread(target=watchdog, daemon=True).start()
    log.info("PredatorSense service started for %s (fan control: %s)", model,
             service.fan_control_problem() or "enabled")
    try:
        server.serve_forever()
    finally:
        stop.set()
        service.shutdown()
        server.server_close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        log.info("PredatorSense service stopped; fans returned to Auto")
    return 0
