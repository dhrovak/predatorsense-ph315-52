import os
import tempfile
import unittest
from unittest import mock

from predator_sense import hardware as hw
from predator_sense import probe
from predator_sense.ec import EC, FakeEC


def make_ec():
    ec = FakeEC()
    ec.mem[hw.CPU_TEMP] = 48
    ec.mem[hw.CPU_RPM], ec.mem[hw.CPU_RPM + 1] = 0x14, 0x0A  # 2580
    ec.mem[hw.GPU_RPM], ec.mem[hw.GPU_RPM + 1] = 0x40, 0x0B  # 2880
    return ec


class FakeSensors:
    """Stands in for the facer sensors parameter; reports nothing unless given values."""

    def __init__(self, **values):
        self.values = dict.fromkeys(hw.WmiSensors.KEYS)
        self.values.update(values)

    def read(self):
        return dict(self.values)


class FakeFans:
    """Stands in for the facer fan_control parameter; mimics its fall-back-to-auto on errors."""

    def __init__(self, available=True):
        self._available = available
        self.state = {"cpu": "auto", "gpu": "auto"}
        self.calls = []
        self.fail = False

    def available(self):
        return self._available

    def get(self):
        return dict(self.state)

    def set(self, cpu, gpu):
        self.calls.append((cpu, gpu))
        if self.fail:
            self.state = {"cpu": "auto", "gpu": "auto"}
            raise OSError("The firmware rejected the fan setting")
        self.state = {"cpu": cpu, "gpu": gpu}


class Env:
    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.ac = os.path.join(self.root, "ac_online")
        with open(self.ac, "w") as f:
            f.write("1\n")

    def add_hwmon(self, index, name, millideg):
        d = os.path.join(self.root, f"hwmon{index}")
        os.makedirs(d)
        with open(os.path.join(d, "name"), "w") as f:
            f.write(name + "\n")
        with open(os.path.join(d, "temp1_input"), "w") as f:
            f.write(f"{millideg}\n")

    def controller(self, ec, fans=None, sensors=None):
        return hw.Controller(ec, fans=fans if fans is not None else FakeFans(),
                             sensors=sensors if sensors is not None else FakeSensors(),
                             hwmon_root=self.root, ac_path=self.ac)


class ParseTests(unittest.TestCase):
    def test_word_is_little_endian(self):
        self.assertEqual(hw.read_word(bytes([0x14, 0x0A]), 0), 2580)

    def test_parse_fan_setting(self):
        self.assertEqual(hw.parse_fan_setting("auto"), "auto")
        self.assertEqual(hw.parse_fan_setting(" MAX "), "max")
        self.assertEqual(hw.parse_fan_setting("60%"), 60)
        self.assertEqual(hw.parse_fan_setting(0), 0)
        self.assertEqual(hw.parse_fan_setting(100), 100)
        for bad in ("101", -1, 101, "fast", None, True, 5.5, "-5", "1 2"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hw.parse_fan_setting(bad)


class WmiFansTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "fan_control")
        with open(self.path, "w") as f:
            f.write("auto auto\n")
        self.fans = hw.WmiFans(self.path)

    def test_set_writes_kernel_format(self):
        self.fans.set(60, "max")
        with open(self.path) as f:
            self.assertEqual(f.read(), "60 max\n")
        self.assertEqual(self.fans.get(), {"cpu": 60, "gpu": "max"})

    def test_set_validates_before_writing(self):
        with self.assertRaises(ValueError):
            self.fans.set(150, "auto")
        with self.assertRaises(ValueError):
            self.fans.set("auto; reboot", "auto")
        with open(self.path) as f:
            self.assertEqual(f.read(), "auto auto\n")

    def test_garbage_value_is_an_error(self):
        with open(self.path, "w") as f:
            f.write("banana\n")
        with self.assertRaises(OSError):
            self.fans.get()

    def test_unavailable(self):
        self.assertFalse(hw.WmiFans(self.path + ".missing").available())


class WmiSensorsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "sensors")

    def read(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        return hw.WmiSensors(self.path).read()

    def test_parses_kernel_format(self):
        values = self.read("supported=0x0207 cpu_temp=55 gpu_temp=61 cpu_rpm=2600 gpu_rpm=0\n")
        self.assertEqual(values, {"cpu_temp": 55, "gpu_temp": 61, "cpu_rpm": 2600, "gpu_rpm": 0})

    def test_unavailable_and_implausible_values_are_none(self):
        values = self.read("supported=0x0000 cpu_temp=- gpu_temp=0 cpu_rpm=65535 gpu_rpm=junk\n")
        self.assertEqual(values, dict.fromkeys(hw.WmiSensors.KEYS))

    def test_missing_parameter(self):
        self.assertEqual(hw.WmiSensors(self.path).read(), dict.fromkeys(hw.WmiSensors.KEYS))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_status(self):
        fans = FakeFans()
        fans.state = {"cpu": 70, "gpu": "auto"}
        s = self.env.controller(make_ec(), fans).status()
        self.assertEqual(s["cpu_temp"], 48)
        self.assertIsNone(s["gpu_temp"])
        self.assertEqual((s["cpu_rpm"], s["gpu_rpm"]), (2580, 2880))
        self.assertEqual(s["sensor_source"], "ec")
        self.assertEqual(s["cpu_fan"], {"mode": "manual", "speed": 70})
        self.assertEqual(s["gpu_fan"], {"mode": "auto", "speed": None})
        self.assertFalse(s["turbo"])
        self.assertTrue(s["on_ac"])

    def test_firmware_sensors_preferred_and_ec_untouched(self):
        ec = make_ec()
        ec.read = lambda addr: self.fail("firmware sensors cover everything; the EC must not be read")
        sensors = FakeSensors(cpu_temp=57, gpu_temp=66, cpu_rpm=3100, gpu_rpm=0)
        s = self.env.controller(ec, sensors=sensors).status()
        self.assertEqual((s["cpu_temp"], s["gpu_temp"], s["cpu_rpm"], s["gpu_rpm"]), (57, 66, 3100, 0))
        self.assertEqual(s["sensor_source"], "firmware")

    def test_partial_firmware_sensors_fall_back_to_ec(self):
        s = self.env.controller(make_ec(), sensors=FakeSensors(gpu_temp=70)).status()
        self.assertEqual((s["gpu_temp"], s["cpu_rpm"], s["gpu_rpm"]), (70, 2580, 2880))

    def test_without_ec_or_firmware_sensors(self):
        s = self.env.controller(None).status()
        self.assertEqual((s["cpu_temp"], s["cpu_rpm"], s["gpu_rpm"]), (None, None, None))

    def test_coretemp_preferred_for_cpu(self):
        self.env.add_hwmon(3, "coretemp", 63500)
        s = self.env.controller(make_ec(), sensors=FakeSensors(cpu_temp=40)).status()
        self.assertEqual(s["cpu_temp"], 64)

    def test_torn_word_read_is_retried(self):
        ec = make_ec()
        ec.mem[hw.CPU_RPM], ec.mem[hw.CPU_RPM + 1] = 0xFF, 0x0F  # 4095
        original = ec.read
        reads = []

        def read(addr):
            reads.append(addr)
            if len(reads) == 2:  # fan speeds up to 4096 right after the high byte was read
                ec.mem[hw.CPU_RPM], ec.mem[hw.CPU_RPM + 1] = 0x00, 0x10
            return original(addr)

        ec.read = read
        self.assertEqual(self.env.controller(ec)._ec_word(hw.CPU_RPM), 4096)

    def test_hwmon_temps(self):
        self.env.add_hwmon(3, "coretemp", 63500)
        self.env.add_hwmon(7, "nouveau", 51000)
        s = self.env.controller(make_ec()).status()
        self.assertEqual((s["cpu_temp"], s["gpu_temp"]), (64, 51))

    def test_set_fan_keeps_other_fan(self):
        fans = FakeFans()
        c = self.env.controller(make_ec(), fans)
        c.set_fan("gpu", "max")
        c.set_fan("cpu", 40)
        self.assertEqual(fans.calls, [("auto", "max"), (40, "max")])

    def test_turbo(self):
        fans = FakeFans()
        c = self.env.controller(make_ec(), fans)
        c.set_turbo(True)
        self.assertTrue(c.status()["turbo"])
        c.set_turbo(False)
        self.assertEqual(fans.state, {"cpu": "auto", "gpu": "auto"})

    def test_ec_access_is_read_only(self):
        self.assertFalse(hasattr(EC, "write"))
        self.assertFalse(hasattr(FakeEC, "write"))

    def test_status_reads_few_ec_registers(self):
        # Each EC read delays the keyboard, so polling must stay cheap.
        ec = make_ec()
        reads = []
        original = ec.read
        ec.read = lambda addr: reads.append(addr) or original(addr)
        ec.read_all = lambda: self.fail("status() must not read the whole EC")
        self.env.controller(ec).status()
        self.assertLessEqual(len(reads), 7)

    def test_invalid_input_sends_nothing(self):
        fans = FakeFans()
        c = self.env.controller(make_ec(), fans)
        with self.assertRaises(ValueError):
            c.set_fan("cpu", 150)
        with self.assertRaises(ValueError):
            c.set_fan("chassis", 50)
        with self.assertRaises(ValueError):
            c.set_both("auto", "fast")
        self.assertEqual(fans.calls, [])

    def test_without_facer_patch(self):
        fans = FakeFans(available=False)
        c = self.env.controller(make_ec(), fans)
        self.assertFalse(c.can_control_fans)
        self.assertIsNone(c.status()["cpu_fan"]["mode"])
        with self.assertRaisesRegex(OSError, "install"):
            c.set_turbo(True)
        self.assertEqual(c.reset(), [])
        self.assertEqual(fans.calls, [])

    def test_reset_reports_errors(self):
        fans = FakeFans()
        fans.fail = True
        self.assertEqual(len(self.env.controller(make_ec(), fans).reset()), 1)

    def test_on_ac_unknown(self):
        c = hw.Controller(make_ec(), fans=FakeFans(), sensors=FakeSensors(), hwmon_root=self.env.root,
                          ac_path="/nonexistent")
        self.assertIsNone(c.status()["on_ac"])

    def test_model_check(self):
        self.assertTrue(hw.is_supported_model("Predator PH315-52"))
        self.assertFalse(hw.is_supported_model("Predator PH315-54"))
        self.assertFalse(hw.is_supported_model(""))

    def test_fingerprint(self):
        files = {hw.DMI_PRODUCT_NAME: "Predator PH315-52\n", hw.DMI_BIOS_VERSION: "V1.12\n"}
        with mock.patch.object(hw, "_read_text", side_effect=lambda p: files.get(p, "").strip() or None):
            self.assertEqual(hw.fingerprint(), {"model": "Predator PH315-52", "bios": "V1.12", "facer": None})


class ProbeReadingTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_measured_registers_pass(self):
        c = self.env.controller(make_ec())
        self.assertEqual(probe.check_readings(c.status(), c.firmware_cpu_temp(), 50), [])

    def test_stopped_fans_pass(self):
        sensors = {"cpu_rpm": 0, "gpu_rpm": 0}
        self.assertEqual(probe.check_readings(sensors, 45, 46), [])

    def test_old_bogus_values_fail(self):
        ec = make_ec()
        ec.mem[hw.CPU_RPM], ec.mem[hw.CPU_RPM + 1] = 0x38, 0x45  # 17720 rpm, as seen with the old map
        ec.mem[hw.CPU_TEMP] = 0
        c = self.env.controller(ec)
        self.assertEqual(len(probe.check_readings(c.status(), c.firmware_cpu_temp(), 55)), 2)

    def test_unreadable_fans_fail(self):
        self.assertEqual(len(probe.check_readings({"cpu_rpm": None, "gpu_rpm": None}, None, 50)), 2)

    def test_smoothed_firmware_temperature_passes(self):
        # Measured on the PH315-52: firmware 57 vs coretemp 48 at idle, 61 vs 72 during a spike.
        sensors = {"cpu_rpm": 2580, "gpu_rpm": 2880}
        self.assertEqual(probe.check_readings(sensors, 57, 48), [])
        self.assertEqual(probe.check_readings(sensors, 61, 72), [])

    def test_temperature_sampling_ignores_a_spike(self):
        self.env.add_hwmon(3, "coretemp", 49000)
        c = self.env.controller(make_ec(), sensors=FakeSensors(cpu_temp=57))
        temp_file = os.path.join(self.env.root, "hwmon3", "temp1_input")
        readings = iter([72000, 49000, 48000, 48000, 49000, 48000])

        def sample():
            with open(temp_file, "w") as f:
                f.write(f"{next(readings)}\n")
            return 57

        c.firmware_cpu_temp = sample
        self.assertEqual(probe.sample_cpu_temps(c, interval=0), (57, 48))


if __name__ == "__main__":
    unittest.main()
