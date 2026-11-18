"""Read-only access to the laptop's Embedded Controller (EC) register space."""

import os

EC_SIZE = 256
EC_PATHS = ("/sys/kernel/debug/ec/ec0/io", "/dev/ec")


class ECError(OSError):
    pass


def _check(addr):
    if not (0 <= addr < EC_SIZE):
        raise ValueError(f"EC address out of range: {addr}")


class EC:
    def __init__(self, path=None):
        self.path = path or self._find()

    @staticmethod
    def _find():
        for path in EC_PATHS:
            if os.path.exists(path):
                return path
        raise ECError("No EC interface found. Is the ec_sys module loaded and debugfs mounted?")

    def read_all(self):
        fd = os.open(self.path, os.O_RDONLY)
        try:
            data = os.pread(fd, EC_SIZE, 0)
        finally:
            os.close(fd)
        if len(data) != EC_SIZE:
            raise ECError(f"Short EC read: got {len(data)} bytes")
        return data

    def read(self, addr):
        _check(addr)
        fd = os.open(self.path, os.O_RDONLY)
        try:
            data = os.pread(fd, 1, addr)
        finally:
            os.close(fd)
        if len(data) != 1:
            raise ECError(f"EC read at {addr:#04x} failed")
        return data[0]


class FakeEC:
    """In-memory EC used by tests and dry runs."""

    def __init__(self, data=None):
        self.mem = bytearray(data if data is not None else bytes(EC_SIZE))

    def read_all(self):
        return bytes(self.mem)

    def read(self, addr):
        _check(addr)
        return self.mem[addr]
