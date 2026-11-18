import unittest
from unittest import mock

from predator_sense import shortcut


class FakeGsettings:
    def __init__(self, bindings="@as []"):
        self.values = {(shortcut.SCHEMA, "custom-keybindings"): bindings}

    def __call__(self, action, schema, key, value=None):
        if action == "get":
            return self.values[(schema, key)]
        if action == "set":
            self.values[(schema, key)] = value
        elif action == "reset":
            self.values.pop((schema, key), None)
        return ""


class ShortcutTests(unittest.TestCase):
    def run_with(self, fake, fn):
        with mock.patch.object(shortcut, "_gsettings", fake):
            fn()

    def test_install_adds_binding_and_keeps_others(self):
        fake = FakeGsettings("['/custom0/']")
        self.run_with(fake, shortcut.install)
        self.assertEqual(fake.values[(shortcut.SCHEMA, "custom-keybindings")],
                         f"['/custom0/', '{shortcut.BINDING_PATH}']")
        relocatable = f"{shortcut.BINDING_SCHEMA}:{shortcut.BINDING_PATH}"
        self.assertEqual(fake.values[(relocatable, "binding")], "XF86Presentation")
        self.assertEqual(fake.values[(relocatable, "command")], shortcut.COMMAND)

    def test_install_twice_does_not_duplicate(self):
        fake = FakeGsettings()
        self.run_with(fake, shortcut.install)
        self.run_with(fake, shortcut.install)
        self.assertEqual(fake.values[(shortcut.SCHEMA, "custom-keybindings")], f"['{shortcut.BINDING_PATH}']")

    def test_remove_keeps_other_bindings(self):
        fake = FakeGsettings(f"['/custom0/', '{shortcut.BINDING_PATH}']")
        self.run_with(fake, shortcut.remove)
        self.assertEqual(fake.values[(shortcut.SCHEMA, "custom-keybindings")], "['/custom0/']")
        self.assertNotIn((f"{shortcut.BINDING_SCHEMA}:{shortcut.BINDING_PATH}", "binding"), fake.values)

    def test_garbage_value_is_an_error(self):
        with self.assertRaises(shortcut.ShortcutError):
            self.run_with(FakeGsettings("__import__('os')"), shortcut.install)

    def test_refuses_root(self):
        with mock.patch.object(shortcut.os, "geteuid", return_value=0):
            self.assertEqual(shortcut.run(), 1)


if __name__ == "__main__":
    unittest.main()
