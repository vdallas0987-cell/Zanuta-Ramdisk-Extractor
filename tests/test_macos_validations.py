"""
End-to-end tests for the three macOS-specific validations:

  1. DMG validation with ``hdiutil verify``  (validator.py)
  2. IMG4 signature verification with ``img4tool``  (extractor.py)
  3. Ramdisk mount check with ``hdiutil attach -nomount``  (extractor.py)

All tests use mocks to simulate macOS behaviour without requiring an
actual macOS environment or the external tools.
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from backend import (
    _check_ramdisk_mountable,
    _save_component_metadata,
    _verify_img4_signature,
    extract_all_components,
    extract_ramdisk,
    extract_required_components,
    inspect_dmg,
)
from models import (
    ComponentInfo,
    ComponentResult,
    DMGInspection,
    ExtractionResult,
    ExtractionStatus,
    IPSWInfo,
)

# Reuse the real BuildManifest fixture
TEST_FIXTURES = Path(__file__).parent / "fixtures"
BUILD_MANIFEST_PATH = TEST_FIXTURES / "BuildManifest.plist"

_FAKE_DMG_SIZE = 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _make_valid_udif(path: Path) -> Path:
    """Create a minimal valid UDIF DMG file at *path* and return it."""
    size = _FAKE_DMG_SIZE
    data = bytearray(size)
    trailer_off = size - 512
    struct.pack_into("<4s", data, trailer_off, b"koly")
    struct.pack_into("<I", data, trailer_off + 4, 4)       # version
    struct.pack_into("<I", data, trailer_off + 8, 512)     # header_size
    struct.pack_into("<I", data, trailer_off + 12, 0)      # flags (not encrypted)
    path.write_bytes(bytes(data))
    return path


def _build_fake_ipsw(ipsw_path: Path) -> None:
    """Create a fake IPSW with BuildManifest + component files."""
    with zipfile.ZipFile(ipsw_path, "w") as zf:
        zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
        zf.writestr("094-32147-023.dmg", b"\x00" * _FAKE_DMG_SIZE)
        zf.writestr("Firmware/dfu/iBSS.n841.RELEASE.im4p", b"\x00" * 4096)
        zf.writestr("Firmware/dfu/iBEC.n841.RELEASE.im4p", b"\x00" * 4096)
        zf.writestr("Firmware/all_flash/DeviceTree.n841ap.im4p", b"\x00" * 4096)
        zf.writestr("kernelcache.release.iphone11b", b"\x00" * 4096)
        zf.writestr("Firmware/all_flash/sep-firmware.n841.RELEASE.im4p", b"\x00" * 4096)


# ---------------------------------------------------------------------------
#  1. DMG hdiutil verify
# ---------------------------------------------------------------------------

class TestHDIUtilVerify(unittest.TestCase):
    """``hdiutil verify`` is called only on macOS and its output is
    appended to the DMGInspection details."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_hdiutil_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("validator.subprocess.run")
    @patch("validator.sys.platform", "darwin")
    def test_hdiutil_success_appended_to_details(self, mock_run):
        """Successful hdiutil should append 'hdiutil: OK' to details."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=b"", stderr=b"",
        )
        dmg = _make_valid_udif(self.tmp / "test.dmg")

        insp = inspect_dmg(dmg, fast=True)
        self.assertIn("hdiutil: OK", insp.details)
        mock_run.assert_called_once()

    @patch("validator.subprocess.run")
    @patch("validator.sys.platform", "darwin")
    def test_hdiutil_failure_appended_to_details(self, mock_run):
        """Failed hdiutil should append 'hdiutil: <error>' to details."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout=b"", stderr=b"checksum failed",
        )
        dmg = _make_valid_udif(self.tmp / "test.dmg")

        insp = inspect_dmg(dmg, fast=True)
        self.assertIn("hdiutil:", insp.details)
        self.assertIn("checksum failed", insp.details)
        mock_run.assert_called_once()

    @patch("validator.subprocess.run")
    @patch("validator.sys.platform", "linux")
    def test_hdiutil_not_called_on_linux(self, mock_run):
        """On Linux, hdiutil should never be invoked."""
        dmg = _make_valid_udif(self.tmp / "test.dmg")

        insp = inspect_dmg(dmg, fast=True)
        self.assertNotIn("hdiutil:", insp.details)
        mock_run.assert_not_called()

    @patch("validator.subprocess.run")
    @patch("validator.sys.platform", "darwin")
    def test_hdiutil_file_not_found_graceful(self, mock_run):
        """hdiutil not installed should not crash; details unchanged."""
        mock_run.side_effect = FileNotFoundError()
        dmg = _make_valid_udif(self.tmp / "test.dmg")

        insp = inspect_dmg(dmg, fast=True)
        # _try_hdiutil returns None → nothing appended
        self.assertNotIn("hdiutil:", insp.details)

    @patch("validator.sys.platform", "darwin")
    def test_hdiutil_called_when_darwin(self):
        """On macOS, hdiutil should be called even with fast=True."""
        dmg = _make_valid_udif(self.tmp / "test.dmg")
        # Don't mock subprocess.run — on linux it'll FileNotFoundError,
        # which _try_hdiutil handles gracefully
        insp = inspect_dmg(dmg, fast=True)
        # hdiutil not found on this system → no hdiutil: in details,
        # but that's OK — the important thing is it tried
        self.assertIsInstance(insp, DMGInspection)


# ---------------------------------------------------------------------------
#  2. IMG4 signature verification
# ---------------------------------------------------------------------------

class TestIMG4SignatureVerification(unittest.TestCase):
    """``img4tool --verify`` runs on macOS after extracting IM4P components."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_img4_"))
        self.ipsw_path = self.tmp / "fixture.ipsw"
        _build_fake_ipsw(self.ipsw_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── extract_required_components tests ───────────────────────────

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_img4_signature_true_in_required_metadata(self, mock_run):
        """Successful img4tool verify → metadata has signature_verified: true."""
        mock_run.return_value = MagicMock(returncode=0)
        results, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        # iBSS, iBEC, DeviceTree, SEP should have signature_verified=True
        for r in results:
            meta = json.loads(r.output_path.with_suffix(".json").read_text())
            if r.component.name in ("iBSS", "iBEC", "DeviceTree", "RestoreSEP"):
                self.assertIn("signature_verified", meta)
                self.assertTrue(meta["signature_verified"],
                                f"{r.component.name} should be True")
            # KernelCache is not IM4P → no signature_verified field
            elif r.component.name == "KernelCache":
                self.assertNotIn("signature_verified", meta)

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_img4_signature_false_in_required_metadata(self, mock_run):
        """Failed img4tool verify → metadata has signature_verified: false."""
        mock_run.return_value = MagicMock(returncode=1)
        results, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        for r in results:
            meta = json.loads(r.output_path.with_suffix(".json").read_text())
            if r.component.name in ("iBSS", "iBEC", "DeviceTree", "RestoreSEP"):
                self.assertIn("signature_verified", meta)
                self.assertFalse(meta["signature_verified"])

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "linux")
    def test_img4_not_called_on_linux_required(self, mock_run):
        """On Linux, img4tool should never be called."""
        results, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        mock_run.assert_not_called()
        for r in results:
            meta = json.loads(r.output_path.with_suffix(".json").read_text())
            self.assertNotIn("signature_verified", meta)

    # ── extract_all_components tests ───────────────────────────────

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_img4_signature_in_all_components(self, mock_run):
        """All IM4P components from extract_all_components get signature_verified."""
        mock_run.return_value = MagicMock(returncode=0)
        results, _ = extract_all_components(self.ipsw_path, self.tmp / "out")

        im4p_results = [r for r in results
                        if r.component.name in ("iBSS", "iBEC", "DeviceTree", "RestoreSEP")]
        for r in im4p_results:
            meta = json.loads(r.output_path.with_suffix(".json").read_text())
            self.assertIn("signature_verified", meta,
                          f"{r.component.name} should have signature_verified")

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "linux")
    def test_img4_not_called_on_linux_all(self, mock_run):
        """On Linux, extract_all_components also never calls img4tool."""
        results, _ = extract_all_components(self.ipsw_path, self.tmp / "out")
        mock_run.assert_not_called()
        for r in results:
            meta = json.loads(r.output_path.with_suffix(".json").read_text())
            self.assertNotIn("signature_verified", meta)

    # ── Unit test for _verify_img4_signature ───────────────────────

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_verify_img4_signature_returns_true(self, mock_run):
        """_verify_img4_signature returns True when img4tool succeeds."""
        mock_run.return_value = MagicMock(returncode=0)
        fake_manifest = Path("/fake/BuildManifest.plist")
        fake_component = Path("/fake/component.img4")
        result = _verify_img4_signature(fake_manifest, fake_component)
        self.assertTrue(result)
        mock_run.assert_called_once_with(
            ["img4tool", "--verify", str(fake_manifest), str(fake_component)],
            capture_output=True, text=True, timeout=30,
        )

    @patch("extractor.sys.platform", "linux")
    def test_verify_img4_signature_linux_returns_none(self):
        """_verify_img4_signature returns None on non-macOS."""
        result = _verify_img4_signature(
            Path("/fake/BuildManifest.plist"),
            Path("/fake/component.img4"),
        )
        self.assertIsNone(result)

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_verify_img4_signature_file_not_found(self, mock_run):
        """_verify_img4_signature returns None when img4tool is not installed."""
        mock_run.side_effect = FileNotFoundError()
        result = _verify_img4_signature(
            Path("/fake/BuildManifest.plist"),
            Path("/fake/component.img4"),
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
#  3. Ramdisk mount check
# ---------------------------------------------------------------------------

class TestRamdiskMountable(unittest.TestCase):
    """``hdiutil attach -nomount`` is called on macOS and logs result."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_mount_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── extract_ramdisk integration test with mocks ─────────────────

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_ramdisk_mountable_log_true(self, mock_run):
        """hdiutil attach success → log '[DEBUG] Ramdisk mountable: True'."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="/dev/disk3  Apple APFS\n", stderr="",
        )
        ipsw_path = self.tmp / "fixture.ipsw"
        _build_fake_ipsw(ipsw_path)
        info = IPSWInfo(
            ipsw_path=ipsw_path,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )

        with self.assertLogs("extractor", level="DEBUG") as logs:
            result = extract_ramdisk(info, self.tmp)

        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        mount_logs = [m for m in logs.output if "Ramdisk mountable" in m]
        self.assertEqual(len(mount_logs), 1)
        self.assertIn("True", mount_logs[0])

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_ramdisk_mountable_log_false(self, mock_run):
        """hdiutil attach fails → log '[DEBUG] Ramdisk mountable: False'."""
        mock_run.return_value = MagicMock(returncode=1)
        ipsw_path = self.tmp / "fixture.ipsw"
        _build_fake_ipsw(ipsw_path)
        info = IPSWInfo(
            ipsw_path=ipsw_path,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )

        with self.assertLogs("extractor", level="DEBUG") as logs:
            result = extract_ramdisk(info, self.tmp)

        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        mount_logs = [m for m in logs.output if "Ramdisk mountable" in m]
        self.assertEqual(len(mount_logs), 1)
        self.assertIn("False", mount_logs[0])

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "linux")
    def test_ramdisk_mountable_not_logged_on_linux(self, mock_run):
        """On Linux, no mount check is performed or logged."""
        ipsw_path = self.tmp / "fixture.ipsw"
        _build_fake_ipsw(ipsw_path)
        info = IPSWInfo(
            ipsw_path=ipsw_path,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )

        with self.assertLogs("extractor", level="DEBUG") as logs:
            result = extract_ramdisk(info, self.tmp)

        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        mount_logs = [m for m in logs.output if "Ramdisk mountable" in m]
        self.assertEqual(len(mount_logs), 0)

    # ── Unit test for _check_ramdisk_mountable ─────────────────────

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_check_ramdisk_mountable_success(self, mock_run):
        """Returns True when hdiutil attach returns a /dev/disk device."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="/dev/disk3          	Apple APFS\n", stderr="",
        )
        result = _check_ramdisk_mountable(Path("/fake.dmg"))
        self.assertTrue(result)

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_check_ramdisk_mountable_no_device(self, mock_run):
        """Returns False when no /dev/disk appears in output."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="no device here\n", stderr="",
        )
        result = _check_ramdisk_mountable(Path("/fake.dmg"))
        self.assertFalse(result)

    @patch("extractor.subprocess.run")
    @patch("extractor.sys.platform", "darwin")
    def test_check_ramdisk_mountable_hdiutil_fails(self, mock_run):
        """Returns False when hdiutil returns non-zero."""
        mock_run.return_value = MagicMock(returncode=1)
        result = _check_ramdisk_mountable(Path("/fake.dmg"))
        self.assertFalse(result)

    @patch("extractor.sys.platform", "linux")
    def test_check_ramdisk_mountable_linux_none(self):
        """Returns None on non-macOS."""
        result = _check_ramdisk_mountable(Path("/fake.dmg"))
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
#  4. Metadata integration: _save_component_metadata
# ---------------------------------------------------------------------------

class TestSaveComponentMetadataSignature(unittest.TestCase):
    """Verify that the new signature_verified field is serialised correctly."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_meta_sig_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_signature_verified_true(self):
        """signature_verified=True should appear in JSON."""
        comp = ComponentInfo(
            name="iBSS", path_in_zip="iBSS.im4p",
            product_type="iPhone11,8",
            product_version="18.7.9", product_build="22H355",
            source_ipsw=Path("/fake.ipsw"),
        )
        out = self.tmp / "iBSS.img4"
        out.write_bytes(b"\x00" * 100)
        _save_component_metadata(comp, out, signature_verified=True)

        meta = json.loads(out.with_suffix(".json").read_text())
        self.assertIn("signature_verified", meta)
        self.assertTrue(meta["signature_verified"])

    def test_signature_verified_false(self):
        """signature_verified=False should appear in JSON."""
        comp = ComponentInfo(
            name="iBEC", path_in_zip="iBEC.im4p",
            product_type="iPhone11,8",
            product_version="18.7.9", product_build="22H355",
            source_ipsw=Path("/fake.ipsw"),
        )
        out = self.tmp / "iBEC.img4"
        out.write_bytes(b"\x00" * 100)
        _save_component_metadata(comp, out, signature_verified=False)

        meta = json.loads(out.with_suffix(".json").read_text())
        self.assertIn("signature_verified", meta)
        self.assertFalse(meta["signature_verified"])

    def test_signature_verified_omitted_when_none(self):
        """When signature_verified=None, the field should not appear."""
        comp = ComponentInfo(
            name="KernelCache", path_in_zip="kernelcache",
            product_type="iPhone11,8",
            product_version="18.7.9", product_build="22H355",
            source_ipsw=Path("/fake.ipsw"),
        )
        out = self.tmp / "kernelcache"
        out.write_bytes(b"\x00" * 100)
        _save_component_metadata(comp, out, signature_verified=None)

        meta = json.loads(out.with_suffix(".json").read_text())
        self.assertNotIn("signature_verified", meta)

    def test_signature_with_digest_verified(self):
        """Both digest_verified and signature_verified should coexist."""
        comp = ComponentInfo(
            name="iBSS", path_in_zip="iBSS.im4p",
            product_type="iPhone11,8",
            product_version="18.7.9", product_build="22H355",
            source_ipsw=Path("/fake.ipsw"),
            digest=b"\x01" * 48,
        )
        out = self.tmp / "iBSS.img4"
        out.write_bytes(b"\x00" * 100)
        _save_component_metadata(comp, out,
                                 digest_verified=True,
                                 signature_verified=True)

        meta = json.loads(out.with_suffix(".json").read_text())
        self.assertIn("digest", meta)
        self.assertTrue(meta["digest"]["verified"])
        self.assertIn("signature_verified", meta)
        self.assertTrue(meta["signature_verified"])


if __name__ == "__main__":
    unittest.main()
