"""
Tests for DMG validation with unittest.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from backend import validate_dmg, inspect_dmg, _check_signatures


class TestCheckSignatures(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_sig_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, content: bytes) -> Path:
        p = self.tmp / f"sig_{len(content)}.bin"
        p.write_bytes(content)
        return p

    def test_detect_udif_koly(self) -> None:
        size = 1024 * 1024
        data = bytearray(size)
        data[size - 512:size - 512 + 4] = b"koly"
        p = self._file(bytes(data))
        fmt = _check_signatures(p, size)
        self.assertEqual(fmt, "UDIF DMG")

    def test_detect_apfs_offset_0(self) -> None:
        p = self._file(b"NXSB" + b"\x00" * 1020)
        fmt = _check_signatures(p, 1024)
        self.assertEqual(fmt, "APFS container")

    def test_detect_apfs_offset_1024(self) -> None:
        p = self._file(b"\x00" * 1024 + b"NXSB" + b"\x00" * 1024)
        fmt = _check_signatures(p, 2048 + 4)
        self.assertEqual(fmt, "APFS container")

    def test_detect_hfs_plus(self) -> None:
        p = self._file(b"\x00" * 1024 + b"H+" + b"\x00" * 1024)
        fmt = _check_signatures(p, 2048 + 2)
        self.assertEqual(fmt, "HFS+")

    def test_detect_hfsx(self) -> None:
        p = self._file(b"\x00" * 1024 + b"HX" + b"\x00" * 1024)
        fmt = _check_signatures(p, 2048 + 2)
        self.assertEqual(fmt, "HFSX (Extended)")

    def test_detect_gpt(self) -> None:
        p = self._file(b"\x00" * 512 + b"EFI PART" + b"\x00" * 1024)
        fmt = _check_signatures(p, 512 + 8 + 1024)
        self.assertEqual(fmt, "GPT header")

    def test_detect_im4p(self) -> None:
        p = self._file(b"IM4P" + b"\x00" * 1024)
        fmt = _check_signatures(p, 1028)
        self.assertEqual(fmt, "IM4P (Image4)")

    def test_detect_im4c(self) -> None:
        p = self._file(b"IM4C" + b"\x00" * 1024)
        fmt = _check_signatures(p, 1028)
        self.assertEqual(fmt, "IM4C (Image4)")

    def test_negative_offset_skips(self) -> None:
        p = self._file(b"koly")
        fmt = _check_signatures(p, 4)
        self.assertIsNone(fmt)


class TestValidateDMG(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_val_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_udif(self) -> None:
        size = 1024 * 1024
        data = bytearray(size)
        data[size - 512:size - 512 + 4] = b"koly"
        p = self.tmp / "valid.dmg"
        p.write_bytes(bytes(data))
        self.assertTrue(validate_dmg(p))

    def test_too_small(self) -> None:
        p = self.tmp / "tiny.dmg"
        p.write_bytes(b"tiny")
        self.assertFalse(validate_dmg(p))

    def test_nonexistent(self) -> None:
        self.assertFalse(validate_dmg(self.tmp / "nonexistent.dmg"))


class TestInspectDMG(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_insp_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_udif(self) -> None:
        size = 1024 * 1024
        data = bytearray(size)
        trailer_off = size - 512
        struct.pack_into("<4s", data, trailer_off, b"koly")
        struct.pack_into("<I", data, trailer_off + 4, 4)     # version
        struct.pack_into("<I", data, trailer_off + 8, 512)   # header_size
        p = self.tmp / "udif.dmg"
        p.write_bytes(bytes(data))
        insp = inspect_dmg(p, fast=True)
        self.assertEqual(insp.format_name, "UDIF DMG")
        self.assertTrue(insp.structure_valid)

    def test_inspect_unknown_format(self) -> None:
        size = 1024 * 1024
        p = self.tmp / "random.bin"
        p.write_bytes(b"\xff" * size)
        insp = inspect_dmg(p, fast=True)
        self.assertIsInstance(insp.format_name, str)
        self.assertFalse(insp.structure_valid)
        self.assertGreater(insp.file_size, 0)

    def test_inspect_nonexistent(self) -> None:
        insp = inspect_dmg(self.tmp / "ghost.dmg", fast=True)
        self.assertFalse(insp.structure_valid)
        self.assertIn("not found", insp.details.lower())


if __name__ == "__main__":
    unittest.main()
