"""
Tests for progressive ZIP reading with progress reporting.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from parser import _ProgressFileWrapper, parse_ipsw


class TestProgressFileWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_prog_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wrapper_reports_progress(self) -> None:
        """Reading a file through the wrapper should fire the callback."""
        p = self.tmp / "test.bin"
        p.write_bytes(b"\x00" * 1024 * 100)  # 100 KB

        calls: list[int] = []
        wrapper = _ProgressFileWrapper(p, progress_callback=lambda pct: calls.append(pct))
        # Read the whole file
        while wrapper.read(4096):
            pass
        wrapper.close()

        self.assertGreater(len(calls), 0)
        self.assertEqual(calls[-1], 100)

    def test_wrapper_with_zipfile(self) -> None:
        """Wrapping a ZIP file should report progress during open."""
        p = self.tmp / "test.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("small.txt", b"hello" * 1000)
            zf.writestr("large.bin", b"\x00" * 1024 * 50)

        calls: list[int] = []
        wrapper = _ProgressFileWrapper(p, progress_callback=lambda pct: calls.append(pct))
        zf = zipfile.ZipFile(wrapper, "r")
        with zf:
            names = zf.namelist()
        wrapper.close()

        self.assertIn("small.txt", names)
        self.assertGreater(len(calls), 0)
        self.assertGreaterEqual(calls[-1], 0)  # at least got some callback

    def test_parse_ipsw_with_progress(self) -> None:
        """parse_ipsw with progress_callback should report scan progress."""
        from tests.test_parser import BUILD_MANIFEST_PATH

        ipsw_path = self.tmp / "test.ipsw"
        with zipfile.ZipFile(ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", b"\x00" * (1024 * 1024))

        calls: list[int] = []
        info = parse_ipsw(ipsw_path, progress_callback=lambda pct: calls.append(pct))

        self.assertEqual(info.product_type, "iPhone11,8")
        self.assertGreater(len(calls), 0)
        # Should have reached 100% by the end
        self.assertEqual(calls[-1], 100)

    def test_parse_ipsw_without_progress_still_works(self) -> None:
        """parse_ipsw without progress_callback should work as before."""
        from tests.test_parser import BUILD_MANIFEST_PATH

        ipsw_path = self.tmp / "test.ipsw"
        with zipfile.ZipFile(ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", b"\x00" * (1024 * 1024))

        info = parse_ipsw(ipsw_path)
        self.assertEqual(info.product_type, "iPhone11,8")


if __name__ == "__main__":
    unittest.main()
