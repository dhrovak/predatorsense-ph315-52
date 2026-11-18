import grp
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from predator_sense import client, daemon, hardware as hw, rgb

from test_hw import FakeFans, FakeSensors, make_ec

FINGERPRINT = {"model": "Predator PH315-52", "bios": "V1.12", "facer": "git.e97bb63-ps2"}


class FakeKeyboard:
    def __init__(self, available=True):
        self._available = available
        self._timeout_available = available
        self.applied = []
        self.timeouts = []

    def available(self):
        return self._available

    def apply(self, settings):
        if not self._available:
            raise OSError("no keyboard")
        self.applied.append(settings)

    def timeout_available(self):
        return self._timeout_available

    def set_timeout(self, on):
        if not self._timeout_available:
            raise OSError("no kb_timeout")
        self.timeouts.append(on)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.verified_file = os.path.join(self.tmp.name, "verified.json")
        self.ec = make_ec()
        self.fans = FakeFans()
        self.sensors = FakeSensors()
        self.keyboard = FakeKeyboard()
        self.controller = hw.Controller(self.ec, fans=self.fans, sensors=self.sensors, hwmon_root=self.tmp.name)
        for name, value in (("detect_model", FINGERPRINT["model"]), ("fingerprint", FINGERPRINT)):
            patcher = mock.patch.object(hw, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def mark_verified(self, **changes):
        with open(self.verified_file, "w") as f:
            json.dump(dict(FINGERPRINT, **changes), f)

    def service(self, verified=True):
        if verified:
            self.mark_verified()
        return daemon.Service(self.controller, self.keyboard, self.state_file, self.verified_file)


class ServiceTests(ServiceTestCase):
    def test_status(self):
        r = self.service().handle({"cmd": "status"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["sensors"]["cpu_rpm"], 2580)
        self.assertTrue(r["data"]["fan_control"])
        self.assertIsNone(r["data"]["fan_control_problem"])
        self.assertIsNone(r["data"]["error"])
        self.assertEqual(r["data"]["rgb"], rgb.DEFAULT_SETTINGS)

    def test_status_without_ec_or_firmware_sensors(self):
        self.controller.ec = None
        r = self.service().handle({"cmd": "status"})
        self.assertTrue(r["ok"])
        self.assertIsNone(r["data"]["sensors"]["cpu_rpm"])
        self.assertIn("cannot be read", r["data"]["error"])

    def test_status_without_ec_uses_firmware_sensors(self):
        self.controller.ec = None
        self.sensors.values.update(cpu_temp=50, gpu_temp=55, cpu_rpm=2000, gpu_rpm=2100)
        data = self.service().handle({"cmd": "status"})["data"]
        self.assertIsNone(data["error"])
        self.assertEqual(data["sensors"]["gpu_temp"], 55)

    def test_without_facer_patch_fans_stay_locked(self):
        self.fans._available = False
        s = self.service(verified=True)
        data = s.handle({"cmd": "status"})["data"]
        self.assertFalse(data["fan_control"])
        self.assertIn("install", data["fan_control_problem"])
        self.assertEqual(data["sensors"]["gpu_rpm"], 2880)
        r = s.handle({"cmd": "set_fans", "cpu": 50, "gpu": 50})
        self.assertFalse(r["ok"])
        self.assertIn("install", r["error"])
        s.restore()
        s.safety_check()
        s.shutdown()
        self.assertEqual(self.fans.calls, [])

    def test_fan_control_locked_until_verified(self):
        s = self.service(verified=False)
        r = s.handle({"cmd": "set_fans", "cpu": 80, "gpu": 80})
        self.assertFalse(r["ok"])
        self.assertIn("verify", r["error"])
        self.assertFalse(s.handle({"cmd": "set_turbo", "on": True})["ok"])
        self.assertEqual(self.fans.calls, [])
        self.mark_verified()
        self.assertTrue(s.handle({"cmd": "set_fans", "cpu": 80, "gpu": 80})["ok"])

    def test_bios_or_module_change_relocks_fan_control(self):
        s = self.service(verified=False)
        self.mark_verified(bios="V1.11")
        problem = s.handle({"cmd": "status"})["data"]["fan_control_problem"]
        self.assertIn("BIOS", problem)
        self.mark_verified(facer="git.e97bb63-ps1")
        self.assertIn("facer", s.handle({"cmd": "set_turbo", "on": True})["error"])
        self.assertEqual(self.fans.calls, [])

    def test_unreadable_verification_locks(self):
        s = self.service(verified=False)
        open(self.verified_file, "w").close()
        self.assertIn("unreadable", s.fan_control_problem())

    def test_set_both_fans_is_one_call_and_persists(self):
        s = self.service()
        self.assertTrue(s.handle({"cmd": "set_fans", "cpu": "65", "gpu": 70})["ok"])
        self.assertEqual(self.fans.calls, [(65, 70)])
        with open(self.state_file) as f:
            self.assertEqual(json.load(f)["fans"], {"cpu": 65, "gpu": 70})

    def test_set_single_fan_keeps_saved_other(self):
        s = self.service()
        s.handle({"cmd": "set_fans", "gpu": "max"})
        s.handle({"cmd": "set_fans", "cpu": 40})
        self.assertEqual(self.fans.calls[-1], (40, "max"))
        self.assertEqual(s.state["fans"], {"cpu": 40, "gpu": "max"})

    def test_low_gpu_speed_refused_without_gpu_temperature(self):
        s = self.service()
        r = s.handle({"cmd": "set_fans", "cpu": 10, "gpu": daemon.BLIND_MIN_SPEED - 1})
        self.assertFalse(r["ok"])
        self.assertIn("GPU temperature", r["error"])
        self.assertEqual(self.fans.calls, [])
        self.assertTrue(s.handle({"cmd": "set_fans", "cpu": 10, "gpu": daemon.BLIND_MIN_SPEED})["ok"])
        self.sensors.values["gpu_temp"] = 60
        self.assertTrue(s.handle({"cmd": "set_fans", "gpu": 10})["ok"])

    def test_turbo(self):
        s = self.service()
        self.assertTrue(s.handle({"cmd": "set_turbo", "on": True})["ok"])
        self.assertTrue(s.handle({"cmd": "status"})["data"]["sensors"]["turbo"])
        self.assertEqual(s.state["fans"], {"cpu": "max", "gpu": "max"})
        self.assertFalse(s.handle({"cmd": "set_turbo", "on": "yes"})["ok"])

    def test_rejects_bad_requests(self):
        s = self.service()
        for req in ([], {"cmd": "rm -rf"}, {"cmd": "set_fans"}, {"cmd": "set_fans", "cpu": 500},
                    {"cmd": "set_fans", "gpu": "1 2"}, {"cmd": "set_fan", "fan": "cpu", "value": 50},
                    {"cmd": "set_rgb", "settings": {"speed": 99}}, {"cmd": ["status"]},
                    {"cmd": "set_rgb", "settings": {"effect": ["wave"]}}):
            self.assertFalse(s.handle(req)["ok"], repr(req))
        self.assertEqual(self.fans.calls, [])
        self.assertEqual(self.keyboard.applied, [])

    def test_firmware_rejection_falls_back_to_auto(self):
        s = self.service()
        s.handle({"cmd": "set_fans", "cpu": 40})
        self.fans.fail = True
        r = s.handle({"cmd": "set_fans", "gpu": 40})
        self.assertFalse(r["ok"])
        self.assertIn("rejected", r["error"])
        self.assertEqual(self.fans.state, {"cpu": "auto", "gpu": "auto"})
        self.assertEqual(s.state["fans"], {"cpu": "auto", "gpu": "auto"})

    def test_set_rgb_merges_and_persists(self):
        s = self.service()
        r = s.handle({"cmd": "set_rgb", "settings": {"effect": "breath", "color": "#00FF00"}})
        self.assertTrue(r["ok"])
        self.assertEqual(self.keyboard.applied[-1]["color"], "#00ff00")
        s.handle({"cmd": "set_rgb", "settings": {"brightness": 20}})
        self.assertEqual(self.keyboard.applied[-1]["effect"], "breath")
        self.assertEqual(self.service().state["rgb"]["brightness"], 20)

    def test_restore_on_start(self):
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": 55, "gpu": "max"},
                       "rgb": rgb.normalize_settings({"effect": "zoom"})}, f)
        self.service().restore()
        self.assertEqual(self.fans.calls, [(55, "max")])
        self.assertEqual(self.keyboard.applied[-1]["effect"], "zoom")
        self.assertEqual(self.keyboard.timeouts, [])

    def test_kb_timeout_set_persists_and_restores(self):
        s = self.service()
        self.assertTrue(s.handle({"cmd": "status"})["data"]["kb_timeout"])
        self.assertTrue(s.handle({"cmd": "set_kb_timeout", "on": False})["ok"])
        self.assertEqual(self.keyboard.timeouts, [False])
        self.assertFalse(s.handle({"cmd": "status"})["data"]["kb_timeout"])
        for bad in ("off", 0, None):
            self.assertFalse(s.handle({"cmd": "set_kb_timeout", "on": bad})["ok"], repr(bad))
        restarted = self.service()
        restarted.restore()
        self.assertEqual(self.keyboard.timeouts, [False, False])

    def test_kb_timeout_without_module_support(self):
        self.keyboard._timeout_available = False
        s = self.service()
        self.assertFalse(s.handle({"cmd": "status"})["data"]["kb_timeout_control"])
        self.assertFalse(s.handle({"cmd": "set_kb_timeout", "on": False})["ok"])
        self.assertIsNone(s.state["kb_timeout"])

    def test_kb_timeout_restored_when_module_appears_and_after_resume(self):
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": "auto", "gpu": "auto"}, "kb_timeout": False}, f)
        self.keyboard._timeout_available = False
        offset = [0.0]
        with mock.patch.object(daemon, "suspend_offset", lambda: offset[0]):
            s = self.service()
            s.restore()
            s.tick()
            self.assertEqual(self.keyboard.timeouts, [])
            self.keyboard._timeout_available = True
            s.tick()
            s.tick()
            self.assertEqual(self.keyboard.timeouts, [False])
            offset[0] = 300.0
            s.tick()
        self.assertEqual(self.keyboard.timeouts, [False, False])

    def test_invalid_kb_timeout_in_state_file_is_ignored(self):
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": "auto", "gpu": "auto"}, "kb_timeout": "no"}, f)
        self.assertIsNone(self.service().state["kb_timeout"])

    def test_restore_raises_low_gpu_speed_without_gpu_temperature(self):
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": 30, "gpu": 10}}, f)
        s = self.service()
        s.restore()
        self.assertEqual(self.fans.calls, [(30, daemon.BLIND_MIN_SPEED)])
        self.assertEqual(s.state["fans"]["gpu"], daemon.BLIND_MIN_SPEED)

    def test_restore_waits_for_late_devices(self):
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": 60, "gpu": 70},
                       "rgb": rgb.normalize_settings({"effect": "neon"})}, f)
        self.fans._available = False
        self.keyboard._available = False
        s = self.service()
        s.restore()
        s.tick()
        self.assertEqual((self.fans.calls, self.keyboard.applied), ([], []))
        self.fans._available = True
        self.keyboard._available = True
        s.tick()
        self.assertEqual(self.fans.calls, [(60, 70)])
        self.assertEqual(self.keyboard.applied[-1]["effect"], "neon")
        s.tick()
        self.assertEqual(len(self.fans.calls), 1)
        self.assertEqual(len(self.keyboard.applied), 1)

    def test_reapplies_after_resume(self):
        offset = [0.0]
        with mock.patch.object(daemon, "suspend_offset", lambda: offset[0]):
            s = self.service()
            s.handle({"cmd": "set_fans", "cpu": 60, "gpu": 70})
            s.handle({"cmd": "set_rgb", "settings": {"effect": "static"}})
            s.tick()
            self.assertEqual(len(self.fans.calls), 1)
            self.fans.state = {"cpu": "auto", "gpu": "auto"}  # firmware reset during suspend
            offset[0] = 300.0
            s.tick()
        self.assertEqual(self.fans.calls[-1], (60, 70))
        self.assertEqual(self.fans.state, {"cpu": 60, "gpu": 70})
        self.assertEqual(len(self.keyboard.applied), 2)

    def test_corrupt_state_file_is_ignored(self):
        with open(self.state_file, "w") as f:
            f.write("{not json")
        self.assertEqual(self.service().state["fans"], {"cpu": "auto", "gpu": "auto"})
        with open(self.state_file, "w") as f:
            json.dump({"fans": {"cpu": 900, "gpu": "auto"}}, f)
        self.assertEqual(self.service().state["fans"]["cpu"], "auto")

    def test_shutdown_resets_fans_but_keeps_saved_settings(self):
        s = self.service()
        s.handle({"cmd": "set_fans", "cpu": 30, "gpu": 40})
        s.shutdown()
        self.assertEqual(self.fans.state, {"cpu": "auto", "gpu": "auto"})
        self.assertEqual(s.state["fans"], {"cpu": 30, "gpu": 40})

    def test_safety_trips_when_hot_in_manual(self):
        s = self.service()
        s.handle({"cmd": "set_fans", "cpu": 20, "gpu": "max"})
        self.ec.mem[hw.CPU_TEMP] = daemon.SAFETY_TEMP
        s.safety_check()
        self.assertEqual(self.fans.state, {"cpu": "auto", "gpu": "auto"})
        self.assertEqual(s.state["fans"], {"cpu": "auto", "gpu": "auto"})
        self.assertIn("Auto", s.handle({"cmd": "status"})["data"]["notice"])

    def test_safety_trips_on_hot_gpu(self):
        s = self.service()
        self.sensors.values["gpu_temp"] = 60
        s.handle({"cmd": "set_fans", "gpu": 20})
        self.sensors.values["gpu_temp"] = daemon.SAFETY_TEMP
        s.safety_check()
        self.assertEqual(self.fans.state, {"cpu": "auto", "gpu": "auto"})

    def test_safety_ignores_hot_in_max(self):
        s = self.service()
        s.handle({"cmd": "set_turbo", "on": True})
        self.ec.mem[hw.CPU_TEMP] = 99
        calls = len(self.fans.calls)
        s.safety_check()
        self.assertEqual(len(self.fans.calls), calls)

    def test_safety_check_reads_nothing_unless_manual(self):
        s = self.service()
        self.ec.read = lambda addr: self.fail("safety check should not touch the EC in auto mode")
        s.safety_check()


class SocketTests(ServiceTestCase):
    def start_server(self, authorize, group=None):
        path = os.path.join(self.tmp.name, "sock")
        server = daemon.Server(path, self.service(), authorize=authorize, group=group)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return path, server

    def test_round_trip(self):
        path, _ = self.start_server(lambda uid: uid == os.getuid())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        data = client.request("status", socket_path=path)
        self.assertEqual(data["sensors"]["gpu_rpm"], 2880)
        client.request("set_fans", socket_path=path, cpu="max")
        self.assertEqual(self.fans.state["cpu"], "max")
        with self.assertRaises(client.ServiceError):
            client.request("set_fans", socket_path=path, cpu=101)

    def test_socket_restricted_to_group(self):
        group = grp.getgrgid(os.getgid()).gr_name
        path, _ = self.start_server(lambda uid: True, group=group)
        st = os.stat(path)
        self.assertEqual((st.st_mode & 0o777, st.st_gid), (0o660, os.getgid()))

    def test_unauthorized_user_is_refused(self):
        path, _ = self.start_server(lambda uid: False)
        with self.assertRaisesRegex(client.ServiceError, "Not allowed"):
            client.request("status", socket_path=path)

    def test_no_socket_permission_explains_group(self):
        path, _ = self.start_server(lambda uid: True)
        os.chmod(path, 0)
        if os.geteuid() == 0:
            self.skipTest("root ignores socket permissions")
        with self.assertRaisesRegex(client.ServiceError, "group"):
            client.request("status", socket_path=path)

    def test_busy_server_refuses_politely(self):
        path, server = self.start_server(lambda uid: True)
        server.slots = threading.BoundedSemaphore(1)
        server.slots.acquire()
        with self.assertRaisesRegex(client.ServiceError, "busy"):
            client.request("status", socket_path=path)

    def test_idle_connection_is_dropped(self):
        path, _ = self.start_server(lambda uid: True)
        with mock.patch.object(daemon._Handler, "timeout", 0.2), \
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(path)
            self.assertEqual(sock.recv(100), b"")

    def test_service_not_running(self):
        with self.assertRaisesRegex(client.ServiceError, "not running"):
            client.request("status", socket_path=os.path.join(self.tmp.name, "missing"))

    def test_peer_uid(self):
        a, b = socket.socketpair(socket.AF_UNIX)
        with a, b:
            self.assertEqual(daemon.peer_uid(a), os.getuid())


class ResetFansTests(unittest.TestCase):
    def test_reset_writes_auto(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fan_control")
            with open(path, "w") as f:
                f.write("40 max\n")
            with mock.patch.object(hw, "FAN_CONTROL_PARAM", path), \
                    mock.patch.object(hw.WmiFans.__init__, "__defaults__", (path,)):
                self.assertEqual(daemon.reset_fans(), 0)
            with open(path) as f:
                self.assertEqual(f.read(), "auto auto\n")

    def test_reset_without_module_is_noop(self):
        with mock.patch.object(hw.WmiFans.__init__, "__defaults__", ("/nonexistent/fan_control",)):
            self.assertEqual(daemon.reset_fans(), 0)


class AuthorizationTests(unittest.TestCase):
    def test_root_always_allowed(self):
        self.assertTrue(daemon.is_authorized(0))

    def test_missing_group_denies(self):
        self.assertFalse(daemon.is_authorized(os.getuid(), group="no-such-group-xyz"))


if __name__ == "__main__":
    unittest.main()
