"""
Tests for IPSW file discovery with unittest.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import find_ipsws, _is_zip_file


class TestIsZipFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_iszip_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_zip(self) -> None:
        p = self.tmp / "valid.zip"
        with zipfile.ZipFile(p, "w"):
            pass
        self.assertTrue(_is_zip_file(p))

    def test_not_zip(self) -> None:
        p = self.tmp / "not.zip"
        p.write_text("not a zip")
        self.assertFalse(_is_zip_file(p))

    def test_nonexistent(self) -> None:
        self.assertFalse(_is_zip_file(self.tmp / "nope.zip"))


class TestFindIPSWs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_find_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_dir(self) -> None:
        self.assertEqual(find_ipsws(str(self.tmp)), [])

    def test_find_single(self) -> None:
        p = self.tmp / "test.ipsw"
        with zipfile.ZipFile(p, "w"):
            pass
        found = find_ipsws(str(self.tmp))
        self.assertEqual(len(found), 1)

    def test_case_insensitive(self) -> None:
        for name in ("a.IPSW", "b.Ipsw", "c.iPsw"):
            p = self.tmp / name
            with zipfile.ZipFile(p, "w"):
                pass
        found = find_ipsws(str(self.tmp))
        self.assertEqual(len(found), 3)

    def test_skip_non_zip(self) -> None:
        p = self.tmp / "fake.ipsw"
        p.write_text("not a zip")
        found = find_ipsws(str(self.tmp))
        self.assertEqual(found, [])

    def test_skip_non_ipsw(self) -> None:
        for name in ("readme.txt", "data.bin"):
            p = self.tmp / name
            with zipfile.ZipFile(p, "w"):
                pass
        self.assertEqual(find_ipsws(str(self.tmp)), [])

    def test_recursive(self) -> None:
        sub = self.tmp / "sub" / "nested"
        sub.mkdir(parents=True)
        p = sub / "deep.ipsw"
        with zipfile.ZipFile(p, "w"):
            pass
        found = find_ipsws(str(self.tmp), recursive=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].resolve(), p.resolve())

    def test_non_recursive(self) -> None:
        sub = self.tmp / "sub"
        sub.mkdir()
        p = sub / "deep.ipsw"
        with zipfile.ZipFile(p, "w"):
            pass
        found = find_ipsws(str(self.tmp), recursive=False)
        self.assertEqual(found, [])

    def test_raises_on_nonexistent(self) -> None:
        with self.assertRaises(NotADirectoryError):
            find_ipsws("/nonexistent/path")

    def test_raises_on_file(self) -> None:
        f = self.tmp / "file.txt"
        f.write_text("hello")
        with self.assertRaises(NotADirectoryError):
            find_ipsws(str(f))

    def test_sorted_output(self) -> None:
        for name in ("c.ipsw", "a.ipsw", "b.ipsw"):
            p = self.tmp / name
            with zipfile.ZipFile(p, "w"):
                pass
        found = find_ipsws(str(self.tmp))
        self.assertEqual([p.name for p in found], ["a.ipsw", "b.ipsw", "c.ipsw"])


if __name__ == "__main__":
    unittest.main()
