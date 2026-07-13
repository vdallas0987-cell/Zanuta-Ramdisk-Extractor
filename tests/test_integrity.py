"""
Tests for integrity validation — digest verification and encryption detection.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from backend import _compute_sha384, _verify_digest, inspect_dmg, _check_signatures


# ── Encryption detection ───────────────────────────────────────────────

class TestEncryptionDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_enc_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_udif(self, flags: int) -> Path:
        """Create a UDIF DMG with a given koly flags value."""
        size = 1024 * 1024
        data = bytearray(size)
        trailer_off = size - 512
        struct.pack_into("<4s", data, trailer_off, b"koly")
        struct.pack_into("<I", data, trailer_off + 4, 4)       # version
        struct.pack_into("<I", data, trailer_off + 8, 512)     # header_size
        struct.pack_into("<I", data, trailer_off + 12, flags)  # flags
        p = self.tmp / "udif.dmg"
        p.write_bytes(bytes(data))
        return p

    def test_not_encrypted(self) -> None:
        """Koly flags without bit 3 = not encrypted."""
        p = self._make_udif(0)
        insp = inspect_dmg(p, fast=True)
        self.assertIsNotNone(insp.encrypted)
        self.assertFalse(insp.encrypted)

    def test_encrypted_flag(self) -> None:
        """Koly flags with bit 3 (0x08) = encrypted."""
        p = self._make_udif(0x08)
        insp = inspect_dmg(p, fast=True)
        self.assertTrue(insp.encrypted)

    def test_encrypted_with_other_flags(self) -> None:
        """Bit 3 set alongside other flags still reports encrypted."""
        p = self._make_udif(0x0F)  # bits 0,1,2,3 all set
        insp = inspect_dmg(p, fast=True)
        self.assertTrue(insp.encrypted)

    def test_encryption_appears_in_details(self) -> None:
        """The word 'ENCRYPTED' should appear in the details when encrypted."""
        p = self._make_udif(0x08)
        insp = inspect_dmg(p, fast=True)
        self.assertIn("ENCRYPTED", insp.details.upper())

    def test_non_udif_format_encryption_none(self) -> None:
        """Non-UDIF formats should have encrypted=None."""
        p = self.tmp / "apfs.bin"
        p.write_bytes(b"NXSB" + b"\x00" * (1024 * 1024 - 4))
        insp = inspect_dmg(p, fast=True)
        self.assertIsNone(insp.encrypted)


# ── Digest verification ───────────────────────────────────────────────

class TestDigestVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_digest2_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compute_sha384_length(self) -> None:
        p = self.tmp / "test.bin"
        p.write_bytes(b"abc" * 1000)
        d = _compute_sha384(p)
        self.assertEqual(len(d), 48)

    def test_compute_sha384_empty_file(self) -> None:
        p = self.tmp / "empty.bin"
        p.write_bytes(b"")
        d = _compute_sha384(p)
        self.assertEqual(len(d), 48)
        # SHA-384 of empty string
        self.assertEqual(d.hex(),
                         "38b060a751ac96384cd9327eb1b1e36"
                         "a21fdb71114be07434c0cc7bf63f6e1d"
                         "a274edebfe76f65fbd51ad2f14898b95b")

    def test_verify_match(self) -> None:
        p = self.tmp / "test.bin"
        p.write_bytes(b"data" * 500)
        h = _compute_sha384(p)
        self.assertTrue(_verify_digest(p, h))

    def test_verify_mismatch(self) -> None:
        p = self.tmp / "test.bin"
        p.write_bytes(b"data" * 500)
        self.assertFalse(_verify_digest(p, b"\x00" * 48))

    def test_verify_no_expected(self) -> None:
        p = self.tmp / "test.bin"
        p.write_bytes(b"data")
        self.assertTrue(_verify_digest(p, None))


# ── DMGInspection new fields ───────────────────────────────────────────

class TestDMGInspectionNewFields(unittest.TestCase):
    def test_digest_verified_default(self) -> None:
        from models import DMGInspection
        insp = DMGInspection(format_name="UDIF DMG", file_size=1024,
                             structure_valid=True)
        self.assertIsNone(insp.digest_verified)
        self.assertIsNone(insp.encrypted)

    def test_encrypted_field_udif(self) -> None:
        """Integration: inspect_dmg should populate encrypted for UDIF DMGs."""
        tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_insp2_"))
        try:
            size = 1024 * 1024
            data = bytearray(size)
            off = size - 512
            struct.pack_into("<4s", data, off, b"koly")
            struct.pack_into("<I", data, off + 4, 4)
            struct.pack_into("<I", data, off + 8, 512)
            struct.pack_into("<I", data, off + 12, 0x08)
            p = tmp / "encrypted.dmg"
            p.write_bytes(bytes(data))
            insp = inspect_dmg(p, fast=True)
            self.assertTrue(insp.encrypted)
            self.assertIn("ENCRYPTED", insp.details.upper())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
