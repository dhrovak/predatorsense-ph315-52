import unittest

from predator_sense import discover


def sample(**regs):
    data = bytearray(256)
    for addr, value in regs.items():
        data[int(addr[1:], 16)] = value
    return bytes(data)


class DiscoverTests(unittest.TestCase):
    def test_stable_changes_ignore_noise(self):
        idle = [sample(x22=0x04, x10=1), sample(x22=0x04, x10=2)]
        turbo = [sample(x22=0x08, x10=3), sample(x22=0x08, x10=9)]
        self.assertEqual(discover.stable_changes(idle, turbo), [(0x22, 0x04, 0x08)])

    def test_rpm_candidates_little_endian(self):
        idle = [sample(x70=0xE8, x71=0x03)]   # 1000
        turbo = [sample(x70=0x94, x71=0x11)]  # 4500
        hits = [h for h in discover.rpm_candidates(idle, turbo) if h[0] == 0x70]
        self.assertEqual(hits, [(0x70, 1000, 4500)])

    def test_temperature_candidates(self):
        samples = [sample(xb0=50, x40=50), sample(xb0=61, x40=50)]
        self.assertEqual(discover.temperature_candidates(samples, [50, 60]), [0xB0])

    def test_hexdump_shape(self):
        lines = discover.hexdump(bytes(range(256))).splitlines()
        self.assertEqual(len(lines), 17)
        self.assertTrue(lines[-1].startswith("f0:  f0 f1"))


if __name__ == "__main__":
    unittest.main()
