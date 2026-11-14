import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autostart


class FakeKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = 'HKCU'
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.error = None

    def _fail(self):
        if self.error:
            raise OSError(self.error)

    def OpenKey(self, root, path, reserved=0, access=0):
        self._fail()
        return FakeKey(self)

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        self._fail()
        return FakeKey(self)

    def QueryValueEx(self, key, name):
        self._fail()
        if name not in self.values:
            raise FileNotFoundError(2, 'not found')
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self._fail()
        self.values[name] = value

    def DeleteValue(self, key, name):
        self._fail()
        if name not in self.values:
            raise FileNotFoundError(2, 'not found')
        del self.values[name]


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        patcher = patch.object(autostart, '_reg', return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_by_default(self):
        self.assertFalse(autostart.enabled())
        self.assertIsNone(autostart.stored_command())

    def test_enable_writes_the_run_value(self):
        with patch.object(autostart.sys, 'frozen', False, create=True):
            self.assertTrue(autostart.enable())
        self.assertEqual(self.registry.values[autostart.VALUE_NAME],
                         autostart.command())
        self.assertTrue(autostart.enabled())

    def test_disable_removes_the_value_and_is_idempotent(self):
        autostart.enable()
        self.assertTrue(autostart.disable())
        self.assertFalse(autostart.enabled())
        self.assertTrue(autostart.disable())

    def test_toggle_flips_the_state(self):
        self.assertEqual(autostart.toggle(), True)
        self.assertTrue(autostart.enabled())
        self.assertEqual(autostart.toggle(), False)
        self.assertFalse(autostart.enabled())

    def test_toggle_reports_none_when_the_registry_refuses(self):
        self.registry.error = 'access denied'
        self.assertIsNone(autostart.toggle())
        self.assertFalse(autostart.enabled())

    def test_enable_failure_is_quiet(self):
        self.registry.error = 'access denied'
        self.assertFalse(autostart.enable())
        self.assertFalse(autostart.enabled())

    def test_refresh_repoints_a_stale_command(self):
        autostart.enable()
        self.registry.values[autostart.VALUE_NAME] = '"C:\\old\\app.exe"'
        with patch.object(autostart, 'command', return_value='"C:\\new\\app.exe"'):
            self.assertTrue(autostart.refresh())
        self.assertEqual(autostart.stored_command(), '"C:\\new\\app.exe"')

    def test_refresh_is_a_noop_when_current(self):
        autostart.enable()
        with patch.object(autostart, 'command', return_value='"C:\\now\\app.exe"'):
            self.assertTrue(autostart.refresh())
        with patch.object(autostart, 'command', return_value='"C:\\now\\app.exe"'):
            self.assertFalse(autostart.refresh())

    def test_refresh_does_nothing_when_disabled(self):
        with patch.object(autostart, 'command') as command:
            self.assertFalse(autostart.refresh())
        command.assert_not_called()

    def test_source_command_uses_pythonw_and_the_script(self):
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / 'python.exe'
            (Path(folder) / 'pythonw.exe').write_bytes(b'')
            with patch.object(autostart.sys, 'frozen', False, create=True), \
                 patch.object(autostart.sys, 'executable', str(python)):
                result = autostart.command()
        self.assertIn('pythonw.exe', result)
        self.assertIn('tools\\tray_app.py', result)
        self.assertTrue(result.startswith('"'))

    def test_frozen_command_is_the_executable(self):
        exe = r'C:\Apps\Touchpad Ink\TouchpadInk.exe'
        with patch.object(autostart.sys, 'frozen', True, create=True), \
             patch.object(autostart.sys, 'executable', exe):
            self.assertEqual(autostart.command(), '"%s"' % exe)


if __name__ == '__main__':
    unittest.main()
