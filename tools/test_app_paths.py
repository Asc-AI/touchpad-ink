"""Path selection for source runs versus a PyInstaller executable."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths


class AppPathTests(unittest.TestCase):
    def test_source_runs_use_the_project_root(self):
        self.assertFalse(getattr(sys, 'frozen', False))
        self.assertEqual(app_paths.app_dir(), Path(__file__).resolve().parent.parent)

    def test_frozen_builds_write_next_to_the_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'TouchpadInk.exe'
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(exe)):
                self.assertEqual(app_paths.app_dir(), Path(folder))
                self.assertEqual(app_paths.data_dir(), Path(folder) / 'data')
                self.assertTrue((Path(folder) / 'data').is_dir())


if __name__ == '__main__':
    unittest.main()
