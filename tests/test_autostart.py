import os
import plistlib
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from netmon import autostart


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # Caminhos explícitos por plataforma: o teste roda igual em Windows, Linux e macOS.
        self.posix_script = PurePosixPath("/opt/app dir/portal_rede.py")
        self.windows_script = PureWindowsPath(r"C:\app dir\portal_rede.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_entry_paths(self):
        env = {"APPDATA": str(self.home / "Roaming")}
        self.assertEqual(
            autostart.entry_path("windows", self.home, env),
            self.home / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "netmon-monitor-rede.cmd",
        )
        self.assertEqual(
            autostart.entry_path("macos", self.home, {}),
            self.home / "Library" / "LaunchAgents" / "com.netmon-monitor-rede.plist",
        )
        self.assertEqual(
            autostart.entry_path("linux", self.home, {}),
            self.home / ".config" / "autostart" / "netmon-monitor-rede.desktop",
        )

    def test_windows_entry(self):
        content = autostart.build_entry("windows", "C:/Py/pythonw.exe", self.windows_script, ["--no-browser"])
        self.assertIn(b'cd /d "C:\\app dir"', content)
        self.assertIn(b'start "" "C:/Py/pythonw.exe" "C:\\app dir\\portal_rede.py"', content)
        self.assertIn(b'"--no-browser"', content)

    def test_macos_entry(self):
        data = plistlib.loads(autostart.build_entry("macos", "/usr/bin/python3", self.posix_script, ["--no-browser"]))
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "/opt/app dir/portal_rede.py", "--no-browser"])
        self.assertEqual(data["WorkingDirectory"], "/opt/app dir")
        self.assertTrue(data["RunAtLoad"])

    def test_linux_entry_quotes_paths(self):
        content = autostart.build_entry("linux", "/usr/bin/python3", self.posix_script, ["--no-browser"]).decode()
        self.assertIn('Exec="/usr/bin/python3" "/opt/app dir/portal_rede.py" "--no-browser"', content)
        self.assertIn("Path=/opt/app dir", content)

    def test_install_and_uninstall_current_platform(self):
        isolated = {"APPDATA": str(self.home / "Roaming"), "XDG_CONFIG_HOME": str(self.home / ".config")}
        with mock.patch.dict(os.environ, isolated):
            path = autostart.install(Path(__file__), ["--no-browser"], home=self.home)
            self.assertTrue(path.exists())
            self.assertTrue(str(path).startswith(str(self.home)))
            self.assertEqual(autostart.uninstall(home=self.home), path)
            self.assertIsNone(autostart.uninstall(home=self.home))


if __name__ == "__main__":
    unittest.main()
