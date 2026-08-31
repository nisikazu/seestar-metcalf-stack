import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SeestarOpenStorageTests(unittest.TestCase):
    def test_release_manifest_includes_launcher_and_script(self):
        manifest = (ROOT / "release-package-manifest.psd1").read_text(encoding="utf-8")

        self.assertIn('"seestar-open-storage.cmd"', manifest)
        self.assertIn('"scripts\\seestar-open-storage.ps1"', manifest)

    def test_launcher_uses_its_own_package_directory(self):
        launcher = (ROOT / "seestar-open-storage.cmd").read_text(encoding="utf-8")

        self.assertIn('%~dp0scripts\\seestar-open-storage.ps1', launcher)
        self.assertIn('-SeestarHost "%~1"', launcher)
        self.assertIn("pause >nul", launcher.lower())

    def test_storage_helper_diagnoses_windows_smb_policy_without_net_view(self):
        helper = (ROOT / "scripts" / "seestar-open-storage.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("Get-SmbClientConfiguration", helper)
        self.assertIn("EnableInsecureGuestLogons", helper)
        self.assertIn("RequireSecuritySignature", helper)
        self.assertIn("RequireEncryption", helper)
        self.assertIn("Test-SmbPath", helper)
        self.assertIn(
            "> Set-SmbClientConfiguration -EnableInsecureGuestLogons $true -Force",
            helper,
        )
        self.assertNotIn("net view ", helper.lower())
        self.assertNotIn("enable Network File Sharing", helper)

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher is Windows-only")
    def test_find_only_accepts_an_explicit_ipv4_without_network_access(self):
        script = ROOT / "scripts" / "seestar-open-storage.ps1"
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-SeestarHost",
                "192.0.2.1",
                "-FindOnly",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("host=192.0.2.1", completed.stdout)
        self.assertIn(r"unc=\\192.0.2.1\EMMC Images\MyWorks", completed.stdout)


if __name__ == "__main__":
    unittest.main()
