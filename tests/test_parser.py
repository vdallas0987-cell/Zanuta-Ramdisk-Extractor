"""
Tests for BuildManifest.plist parsing with unittest.
"""

from __future__ import annotations

import plistlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import _extract_plist, _find_ramdisk_in_manifest, _get_device_info, parse_ipsw

TEST_FIXTURES = Path(__file__).parent / "fixtures"
BUILD_MANIFEST_PATH = TEST_FIXTURES / "BuildManifest.plist"


class TestExtractPlist(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_plist_"))
        self.ipsw_path = self.tmp / "fixture.ipsw"
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", b"\x00" * (1024 * 1024))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract(self) -> None:
        with zipfile.ZipFile(self.ipsw_path, "r") as zf:
            manifest = _extract_plist(zf)
        self.assertIsInstance(manifest, dict)
        self.assertIn("BuildIdentities", manifest)
        self.assertIn("ProductVersion", manifest)
        self.assertIn("ProductBuildVersion", manifest)

    def test_root_values(self) -> None:
        with zipfile.ZipFile(self.ipsw_path, "r") as zf:
            manifest = _extract_plist(zf)
        self.assertEqual(manifest["ProductVersion"], "18.7.9")
        self.assertEqual(manifest["ProductBuildVersion"], "22H355")


class TestFindRamdiskInManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = plistlib.loads(BUILD_MANIFEST_PATH.read_bytes())

    def test_returns_ramdisk_path(self) -> None:
        identity = self.manifest["BuildIdentities"][0]
        path = _find_ramdisk_in_manifest(identity)
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(".dmg"))

    def test_correct_ramdisk(self) -> None:
        identity = self.manifest["BuildIdentities"][0]
        path = _find_ramdisk_in_manifest(identity)
        self.assertEqual(path, "094-32147-023.dmg")


class TestGetDeviceInfo(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = plistlib.loads(BUILD_MANIFEST_PATH.read_bytes())

    def test_returns_valid_info(self) -> None:
        info, _identity = _get_device_info(self.manifest)
        self.assertIsNotNone(info)
        self.assertEqual(info["product_type"], "iPhone11,8")
        self.assertEqual(info["version"], "18.7.9")
        self.assertEqual(info["build"], "22H355")
        self.assertEqual(info["ramdisk_path"], "094-32147-023.dmg")

    def test_prefers_erase_over_update(self) -> None:
        manifest = {
            "ProductVersion": "1.0",
            "ProductBuildVersion": "1A1",
            "BuildIdentities": [
                {
                    "Ap,ProductType": "iPhone11,8",
                    "Info": {"RestoreBehavior": "Update"},
                    "Manifest": {"RestoreRamDisk": {"Info": {"Path": "update.dmg"}}},
                },
                {
                    "Ap,ProductType": "iPhone11,8",
                    "Info": {"RestoreBehavior": "Erase"},
                    "Manifest": {"RestoreRamDisk": {"Info": {"Path": "erase.dmg"}}},
                },
            ],
        }
        info, _identity = _get_device_info(manifest)
        self.assertEqual(info["ramdisk_path"], "erase.dmg")


class TestParseIPSW(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_parse_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_ipsw(self, name: str, manifest: dict,
                   files: dict[str, bytes] | None = None) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("BuildManifest.plist", plistlib.dumps(manifest))
            if files:
                for fname, content in files.items():
                    zf.writestr(fname, content)
        return path

    def test_parse_valid(self) -> None:
        manifest = plistlib.loads(BUILD_MANIFEST_PATH.read_bytes())
        path = self._make_ipsw("valid.ipsw", manifest,
                               {"094-32147-023.dmg": b"\x00" * (1024 * 1024)})
        info = parse_ipsw(path)
        self.assertEqual(info.product_type, "iPhone11,8")
        self.assertEqual(info.product_version, "18.7.9")
        self.assertEqual(info.product_build, "22H355")
        self.assertIn("iPhone XR", info.display_name)

    def test_parse_non_a12_a13_device(self) -> None:
        """parse_ipsw does NOT filter by A12/A13 — the caller does."""
        manifest = {
            "ProductVersion": "1.0", "ProductBuildVersion": "1A1",
            "BuildIdentities": [{
                "Ap,ProductType": "iPhone14,2",
                "Manifest": {"RestoreRamDisk": {"Info": {"Path": "r.dmg"}}},
            }],
        }
        path = self._make_ipsw("unsupported.ipsw", manifest, {"r.dmg": b"\x00" * 1024})
        info = parse_ipsw(path)
        self.assertEqual(info.product_type, "iPhone14,2")

    def test_parse_nonexistent_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            parse_ipsw(Path("/nonexistent/ipsw.ipsw"))

    def test_parse_corrupt_raises(self) -> None:
        path = self.tmp / "corrupt.ipsw"
        path.write_text("not a zip")
        with self.assertRaises(ValueError):
            parse_ipsw(path)

    def test_parse_empty_zip_raises(self) -> None:
        path = self.tmp / "empty.ipsw"
        with zipfile.ZipFile(path, "w"):
            pass
        with self.assertRaises(ValueError):
            parse_ipsw(path)


if __name__ == "__main__":
    unittest.main()
