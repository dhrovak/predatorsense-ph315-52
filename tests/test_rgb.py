import os
import tempfile
import unittest

from predator_sense import rgb


def facer_rgb_payload(mode, zone=1, speed=4, brightness=100, direction=1, red=50, green=255, blue=50):
    """Reference copy of the byte layout in facer_rgb.py (commit e97bb63)."""
    if mode == 0:
        static = [0] * 4
        static[0] = 1 << (zone - 1)
        static[1], static[2], static[3] = red, green, blue
        dynamic = [0] * 16
        dynamic[2] = brightness
        dynamic[9] = 1
        return bytes(static), bytes(dynamic)
    dynamic = [0] * 16
    dynamic[0] = mode
    dynamic[1] = speed
    dynamic[2] = brightness
    dynamic[3] = 8 if mode == 3 else 0
    dynamic[4] = direction
    dynamic[5], dynamic[6], dynamic[7] = red, green, blue
    dynamic[9] = 1
    return None, bytes(dynamic)


class PayloadTests(unittest.TestCase):
    def test_dynamic_effects_match_facer(self):
        for name, mode in rgb.EFFECTS.items():
            if name == "static":
                continue
            for direction_name, direction in rgb.DIRECTIONS.items():
                s = rgb.normalize_settings({"effect": name, "speed": 7, "brightness": 80,
                                            "direction": direction_name, "color": "#123456"})
                has_color = name in rgb.EFFECTS_WITH_COLOR
                _, expected = facer_rgb_payload(mode, speed=7, brightness=80, direction=direction,
                                                red=0x12 if has_color else 0,
                                                green=0x34 if has_color else 0,
                                                blue=0x56 if has_color else 0)
                self.assertEqual(rgb.effect_payload(s), expected, f"{name}/{direction_name}")

    def test_static_matches_facer(self):
        colors = ["#ff0000", "#00ff00", "#0000ff", "#ffffff"]
        s = rgb.normalize_settings({"effect": "static", "brightness": 60, "zones": colors})
        for i, color in enumerate(colors):
            r, g, b = rgb.color_bytes(color)
            static, commit = facer_rgb_payload(0, zone=i + 1, brightness=60, red=r, green=g, blue=b)
            self.assertEqual(rgb.zone_payload(i, color), static)
            self.assertEqual(rgb.effect_payload(s), commit)

    def test_validation(self):
        bad = [
            {"effect": "rainbow"},
            {"brightness": 101},
            {"brightness": "50"},
            {"speed": 10},
            {"speed": True},
            {"direction": "up"},
            {"effect": ["wave"]},
            {"direction": {}},
            {"color": "red"},
            {"color": "#12345"},
            {"zones": ["#ffffff"] * 3},
            {"zones": "#ffffff"},
            {"unknown": 1},
        ]
        for settings in bad:
            with self.assertRaises(ValueError, msg=repr(settings)):
                rgb.normalize_settings(settings)
        with self.assertRaises(ValueError):
            rgb.normalize_settings("wave")

    def test_normalize_merges_and_lowercases(self):
        base = rgb.normalize_settings({"effect": "static", "brightness": 30})
        s = rgb.normalize_settings({"color": "ABCDEF"}, base=base)
        self.assertEqual(s["effect"], "static")
        self.assertEqual(s["brightness"], 30)
        self.assertEqual(s["color"], "#abcdef")


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dyn = os.path.join(self.tmp.name, "dyn")
        self.sta = os.path.join(self.tmp.name, "sta")

    def test_unavailable(self):
        kb = rgb.Keyboard(self.dyn, self.sta)
        self.assertFalse(kb.available())
        with self.assertRaises(OSError):
            kb.apply(rgb.normalize_settings({}))

    def test_static_writes_all_zones_then_commit(self):
        written = []

        class Recorder(rgb.Keyboard):
            def available(self):
                return True

            @staticmethod
            def _write(path, payload):
                written.append((path, payload))

        kb = Recorder(self.dyn, self.sta)
        s = rgb.normalize_settings({"effect": "static", "zones": ["#010203"] * 4})
        kb.apply(s)
        self.assertEqual([p for p, _ in written], [self.sta] * 4 + [self.dyn])
        self.assertEqual(written[3][1], bytes([8, 1, 2, 3]))
        self.assertEqual(written[4][1], rgb.effect_payload(s))

    def test_writes_to_device_files(self):
        open(self.dyn, "wb").close()
        open(self.sta, "wb").close()
        kb = rgb.Keyboard(self.dyn, self.sta)
        s = rgb.normalize_settings({"effect": "breath", "color": "#ff00ff"})
        kb.apply(s)
        with open(self.dyn, "rb") as f:
            self.assertEqual(f.read(), rgb.effect_payload(s))


if __name__ == "__main__":
    unittest.main()
