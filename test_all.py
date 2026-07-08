#!/usr/bin/env python3
"""
Automated test suite for Zanuta Ramdisk Extractor.

Run with::

    source venv/bin/activate
    python3 test_all.py             # quick run
    python3 test_all.py -v          # verbose
    python3 test_all.py --coverage  # with coverage report (requires coverage)

Exit code: 0 if all pass, 1 otherwise.
"""

from __future__ import annotations

import io
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

# ── Ensure the project root is on sys.path ─────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from models import (
    IPSWInfo,
    ExtractionResult,
    ExtractionStatus,
    Stats,
    is_a12_a13,
    A12_A13_DEVICES,
    DEVICE_NAMES,
)
from backend import (
    find_ipsw_files,
    parse_ipsw,
    validate_dmg,
    extract_ramdisk,
    process_all,
    _unique_path,
)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════

def _make_minimal_ipsw(
    path: Path,
    product_type: str = "iPhone12,1",
    product_version: str = "14.3",
    product_build: str = "18C66",
    ramdisk_size: int = 1024 * 1024 + 512,  # 1 MB + trailer
    corrupt_koly: bool = False,
) -> None:
    """Create a minimal .ipsw (ZIP) with BuildManifest and a dummy ramdisk.

    Parameters
    ----------
    corrupt_koly
        If True, writes ``koly`` at the wrong offset so validation fails.
    """
    manifest = {
        "ProductVersion": product_version,
        "ProductBuildVersion": product_build,
        "BuildIdentities": [
            {
                "Info": {
                    "ProductType": product_type,
                    "ProductVersion": product_version,
                    "ProductBuildVersion": product_build,
                },
                "Manifest": {
                    "Restore": {
                        "RestoreRamDisk": {
                            "Info": {"Path": "ramdisk.dmg"},
                        },
                    },
                },
            },
        ],
    }

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BuildManifest.plist", plistlib.dumps(manifest))
        # DMG: at least 1 MB + 512-byte trailer
        dmg_data = bytearray(ramdisk_size)
        trailer_offset = ramdisk_size - 512
        if corrupt_koly:
            dmg_data[trailer_offset:] = b"\x00" * 512  # no koly
        else:
            dmg_data[trailer_offset:trailer_offset + 4] = b"koly"
        zf.writestr("ramdisk.dmg", bytes(dmg_data))


def _make_dmg_with_koly(path: Path, size: int = 2 * 1024 * 1024) -> Path:
    """Write a valid DMG (with ``koly`` trailer) to *path*."""
    data = bytearray(size)
    trailer_offset = size - 512
    data[trailer_offset:trailer_offset + 4] = b"koly"
    path.write_bytes(data)
    return path


class _TempDir:
    """Context manager that creates + destroys a temporary directory."""

    def __init__(self) -> None:
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="ramdisk_test_"))
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
#  Tests: models.py
# ══════════════════════════════════════════════════════════════════════════

class TestModels(unittest.TestCase):
    """Data classes, enums, constants, and helper functions."""

    def test_is_a12_a13_known(self) -> None:
        for device in A12_A13_DEVICES:
            self.assertTrue(is_a12_a13(device), f"{device} should match")

    def test_is_a12_a13_unknown(self) -> None:
        for pid in ("iPhone10,6", "iPhone13,1", "iPad8,1", ""):
            self.assertFalse(is_a12_a13(pid), f"{pid} should NOT match")

    def test_is_a12_a13_case_sensitive(self) -> None:
        self.assertFalse(is_a12_a13("iphone12,1"))

    def test_device_names_exist_for_all_devices(self) -> None:
        for device in A12_A13_DEVICES:
            self.assertIn(device, DEVICE_NAMES,
                          f"{device} missing from DEVICE_NAMES")

    def test_ipsw_info_default_device_name(self) -> None:
        """Unknown product_type gets itself as device_name."""
        info = IPSWInfo(Path("/f.ipsw"), "Foo,1", "1.0", "1A", "r.dmg")
        self.assertEqual(info.device_name, "Foo,1")

    def test_ipsw_info_display_name(self) -> None:
        info = IPSWInfo(Path("/f.ipsw"), "iPhone12,1", "14.3", "18C66", "r.dmg")
        self.assertIn("iPhone 11", info.display_name)
        self.assertIn("iPhone12,1", info.display_name)

    def test_ipsw_info_output_filename(self) -> None:
        info = IPSWInfo(Path("/f.ipsw"), "iPhone12,1", "14.3", "18C66", "r.dmg")
        self.assertEqual(info.output_filename,
                         "iPhone12,1_14.3_18C66_ramdisk.dmg")

    def test_ipsw_info_output_relative_path(self) -> None:
        info = IPSWInfo(Path("/f.ipsw"), "iPhone12,1", "14.3", "18C66", "r.dmg")
        expected = Path("iPhone12,1") / "14.3" / "iPhone12,1_14.3_18C66_ramdisk.dmg"
        self.assertEqual(info.output_relative_path, expected)

    def test_extraction_status_enum_values(self) -> None:
        """All expected statuses exist and are unique."""
        names = {s.name for s in ExtractionStatus}
        for expected in ("PENDING", "SCANNING", "READY", "EXTRACTING",
                         "SUCCESS", "ERROR", "SKIPPED"):
            self.assertIn(expected, names)

    def test_stats_defaults(self) -> None:
        s = Stats()
        self.assertEqual(s.total, 0)
        self.assertEqual(s.success, 0)
        self.assertEqual(s.skipped, 0)
        self.assertEqual(s.error, 0)

    def test_stats_completed(self) -> None:
        s = Stats(total=10, success=5, skipped=2, error=1)
        self.assertEqual(s.completed, 8)

    def test_stats_str(self) -> None:
        s = Stats(total=10, success=5, skipped=2, error=1)
        text = str(s)
        self.assertIn("5", text)
        self.assertIn("2", text)
        self.assertIn("1", text)

    def test_stats_to_dict(self) -> None:
        s = Stats(total=10, success=5, skipped=2, error=1)
        self.assertEqual(s.to_dict(),
                         {"total": 10, "success": 5, "skipped": 2, "error": 1})


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — scanning
# ══════════════════════════════════════════════════════════════════════════

class TestBackendScan(unittest.TestCase):
    """Directory scanning for .ipsw files."""

    def test_empty_directory(self) -> None:
        with _TempDir() as tmp:
            self.assertEqual(find_ipsw_files(tmp), [])

    def test_finds_ipsw_files(self) -> None:
        with _TempDir() as tmp:
            (tmp / "a.ipsw").touch()
            (tmp / "b.ipsw").touch()
            (tmp / "readme.txt").touch()
            self.assertEqual(len(find_ipsw_files(tmp)), 2)

    def test_recursive_scan(self) -> None:
        with _TempDir() as tmp:
            sub = tmp / "sub"
            sub.mkdir()
            (sub / "nested.ipsw").touch()
            self.assertEqual(len(find_ipsw_files(tmp)), 1)

    def test_sorted_output(self) -> None:
        with _TempDir() as tmp:
            (tmp / "z.ipsw").touch()
            (tmp / "a.ipsw").touch()
            files = find_ipsw_files(tmp)
            self.assertEqual(files[0].name, "a.ipsw")
            self.assertEqual(files[1].name, "z.ipsw")

    def test_nonexistent_directory(self) -> None:
        with _TempDir() as tmp:
            with self.assertRaises(NotADirectoryError):
                find_ipsw_files(tmp / "ghost")

    def test_file_instead_of_directory(self) -> None:
        with _TempDir() as tmp:
            f = tmp / "file.txt"
            f.touch()
            with self.assertRaises(NotADirectoryError):
                find_ipsw_files(f)


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — parsing
# ══════════════════════════════════════════════════════════════════════════

class TestBackendParse(unittest.TestCase):
    """BuildManifest.plist parsing."""

    def test_parse_valid_a12(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "test.ipsw"
            _make_minimal_ipsw(p)
            info = parse_ipsw(p)
            self.assertEqual(info.product_type, "iPhone12,1")
            self.assertEqual(info.product_version, "14.3")
            self.assertEqual(info.product_build, "18C66")
            self.assertIn("ramdisk.dmg", info.ramdisk_path)

    def test_parse_returns_different_devices(self) -> None:
        with _TempDir() as tmp:
            for pid in ("iPhone11,2", "iPhone12,5", "iPhone12,8"):
                p = tmp / f"{pid}.ipsw"
                _make_minimal_ipsw(p, product_type=pid)
                info = parse_ipsw(p)
                self.assertEqual(info.product_type, pid)

    def test_parse_file_not_found(self) -> None:
        with _TempDir() as tmp:
            with self.assertRaises(FileNotFoundError):
                parse_ipsw(tmp / "nonexistent.ipsw")

    def test_parse_corrupt_zip(self) -> None:
        with _TempDir() as tmp:
            f = tmp / "bad.ipsw"
            f.write_text("not a zip file")
            with self.assertRaises(ValueError):
                parse_ipsw(f)

    def test_parse_empty_zip(self) -> None:
        with _TempDir() as tmp:
            f = tmp / "empty.ipsw"
            with zipfile.ZipFile(f, "w") as zf:
                pass  # no BuildManifest inside
            with self.assertRaises(ValueError):
                parse_ipsw(f)

    def test_parse_non_a12_succeeds(self) -> None:
        """Parsing succeeds for devices outside the target range."""
        with _TempDir() as tmp:
            p = tmp / "old.ipsw"
            _make_minimal_ipsw(p, product_type="iPhone10,6")
            info = parse_ipsw(p)
            self.assertEqual(info.product_type, "iPhone10,6")
            self.assertFalse(is_a12_a13(info.product_type))


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — DMG validation
# ══════════════════════════════════════════════════════════════════════════

class TestBackendValidateDMG(unittest.TestCase):
    """Apple Disk Image (``koly`` trailer) validation."""

    def test_valid_dmg_with_koly(self) -> None:
        with _TempDir() as tmp:
            p = _make_dmg_with_koly(tmp / "valid.dmg")
            self.assertTrue(validate_dmg(p))

    def test_valid_dmg_different_sizes(self) -> None:
        with _TempDir() as tmp:
            for size in (2 * 1024 * 1024, 5 * 1024 * 1024, 10 * 1024 * 1024):
                p = _make_dmg_with_koly(tmp / f"dmg_{size}.dmg", size=size)
                self.assertTrue(validate_dmg(p),
                                f"DMG of size {size} should be valid")

    def test_invalid_dmg_too_small(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "small.dmg"
            p.write_bytes(b"\x00" * 1024)  # << 1 MB minimum
            self.assertFalse(validate_dmg(p))

    def test_invalid_dmg_exactly_under_min(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "almost.dmg"
            p.write_bytes(b"\x00" * (1024 * 1024 - 1))
            self.assertFalse(validate_dmg(p))

    def test_invalid_dmg_no_koly(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "no_koly.dmg"
            data = b"\x00" * (2 * 1024 * 1024)
            p.write_bytes(data)
            self.assertFalse(validate_dmg(p))

    def test_nonexistent_file(self) -> None:
        with _TempDir() as tmp:
            self.assertFalse(validate_dmg(tmp / "ghost.dmg"))


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — _unique_path
# ══════════════════════════════════════════════════════════════════════════

class TestUniquePath(unittest.TestCase):
    """Automatic collision avoidance for output filenames."""

    def test_no_collision(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "test.dmg"
            self.assertEqual(_unique_path(p), p)

    def test_one_collision(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "test.dmg"
            p.touch()
            self.assertEqual(_unique_path(p), tmp / "test_1.dmg")

    def test_multiple_collisions(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "test.dmg"
            p.touch()
            (tmp / "test_1.dmg").touch()
            (tmp / "test_2.dmg").touch()
            self.assertEqual(_unique_path(p), tmp / "test_3.dmg")

    def test_preserves_suffix(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "file.dmg"
            p.touch()
            result = _unique_path(p)
            self.assertEqual(result.suffix, ".dmg")

    def test_handles_dotted_stems(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "iPhone12,1_14.3_18C66_ramdisk.dmg"
            p.touch()
            result = _unique_path(p)
            self.assertEqual(result.name,
                             "iPhone12,1_14.3_18C66_ramdisk_1.dmg")


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — extraction
# ══════════════════════════════════════════════════════════════════════════

class TestBackendExtract(unittest.TestCase):
    """Single ramdisk extraction from an IPSW."""

    def test_extract_success(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            result = extract_ramdisk(info, out)
            self.assertEqual(result.status, ExtractionStatus.SUCCESS)
            self.assertIsNotNone(result.output_path)
            self.assertTrue(result.output_path.is_file())
            self.assertGreater(result.output_path.stat().st_size, 0)

    def test_extract_output_structure(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            result = extract_ramdisk(info, out)
            expected = out / info.output_relative_path
            self.assertEqual(result.output_path, expected)

    def test_extract_sets_correct_message(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            result = extract_ramdisk(info, out)
            self.assertIn("Extracted to", result.message)

    def test_extract_twice_does_not_overwrite(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            r1 = extract_ramdisk(info, out)
            r2 = extract_ramdisk(info, out)
            self.assertEqual(r1.status, ExtractionStatus.SUCCESS)
            self.assertEqual(r2.status, ExtractionStatus.SUCCESS)
            self.assertNotEqual(r1.output_path, r2.output_path)
            self.assertIn("_1", r2.output_path.name)

    def test_extract_twice_increments(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            r1 = extract_ramdisk(info, out)
            r2 = extract_ramdisk(info, out)
            r3 = extract_ramdisk(info, out)
            self.assertIn("_1", r2.output_path.name)
            self.assertIn("_2", r3.output_path.name)

    def test_extract_corrupt_zip_returns_error(self) -> None:
        with _TempDir() as tmp:
            ipsw = tmp / "bad.ipsw"
            ipsw.write_text("not a zip")
            out = tmp / "out"
            out.mkdir()
            info = IPSWInfo(ipsw, "iPhone12,1", "14.3", "18C66", "ramdisk.dmg")
            result = extract_ramdisk(info, out)
            self.assertEqual(result.status, ExtractionStatus.ERROR)

    def test_extract_validates_dmg(self) -> None:
        """If the extracted DMG lacks a koly trailer, it is rejected."""
        with _TempDir() as tmp:
            ipsw = tmp / "bad_dmg.ipsw"
            _make_minimal_ipsw(ipsw, corrupt_koly=True)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            result = extract_ramdisk(info, out)
            self.assertEqual(result.status, ExtractionStatus.ERROR)

    def test_extract_validates_dmg_removes_file(self) -> None:
        """Failed extraction should clean up the invalid file."""
        with _TempDir() as tmp:
            ipsw = tmp / "bad_dmg.ipsw"
            _make_minimal_ipsw(ipsw, corrupt_koly=True)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            result = extract_ramdisk(info, out)
            if result.output_path:
                self.assertFalse(result.output_path.exists())

    def test_progress_callback_is_called(self) -> None:
        """The progress callback should receive values during extraction."""
        with _TempDir() as tmp:
            ipsw = tmp / "test.ipsw"
            _make_minimal_ipsw(ipsw, ramdisk_size=5 * 1024 * 1024)
            out = tmp / "out"
            out.mkdir()
            info = parse_ipsw(ipsw)
            values = []

            def cb(pct: int) -> None:
                values.append(pct)

            extract_ramdisk(info, out, progress_callback=cb)
            self.assertGreater(len(values), 0)
            self.assertEqual(values[-1], 100)


# ══════════════════════════════════════════════════════════════════════════
#  Tests: backend.py — batch processing
# ══════════════════════════════════════════════════════════════════════════

class TestBackendBatch(unittest.TestCase):
    """``process_all`` orchestration layer."""

    def test_empty_list(self) -> None:
        with _TempDir() as tmp:
            out = tmp / "out"
            out.mkdir()
            results, stats = process_all([], out)
            self.assertEqual(len(results), 0)
            self.assertEqual(stats.total, 0)

    def test_all_success(self) -> None:
        with _TempDir() as tmp:
            out = tmp / "out"
            out.mkdir()
            paths = []
            for i in range(3):
                p = tmp / f"d{i}.ipsw"
                _make_minimal_ipsw(p)
                paths.append(p)
            results, stats = process_all(paths, out)
            self.assertEqual(stats.total, 3)
            self.assertEqual(stats.success, 3)
            self.assertEqual(len(results), 3)

    def test_mixed_devices(self) -> None:
        """A12 + non-A12 + corrupt → appropriate stats."""
        with _TempDir() as tmp:
            out = tmp / "out"
            out.mkdir()
            a12 = tmp / "a12.ipsw"
            _make_minimal_ipsw(a12, product_type="iPhone12,1")
            old = tmp / "old.ipsw"
            _make_minimal_ipsw(old, product_type="iPhone10,6")
            bad = tmp / "bad.ipsw"
            bad.write_text("corrupt")
            results, stats = process_all([a12, old, bad], out)
            self.assertEqual(stats.total, 3)
            self.assertEqual(stats.success, 1)
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(stats.error, 1)

    def test_callbacks_are_fired(self) -> None:
        with _TempDir() as tmp:
            out = tmp / "out"
            out.mkdir()
            paths = []
            for i in range(2):
                p = tmp / f"d{i}.ipsw"
                _make_minimal_ipsw(p)
                paths.append(p)
            items = []
            progress_log = []

            def on_item(r):
                items.append(r)

            def on_progress(c, t):
                progress_log.append((c, t))

            process_all(paths, out,
                        item_callback=on_item,
                        progress_callback=on_progress)
            self.assertEqual(len(items), 2)
            self.assertEqual(len(progress_log), 2)
            self.assertEqual(progress_log[-1], (2, 2))

    def test_skipped_non_a12(self) -> None:
        with _TempDir() as tmp:
            out = tmp / "out"
            out.mkdir()
            p = tmp / "old.ipsw"
            _make_minimal_ipsw(p, product_type="iPhone10,6")
            results, stats = process_all([p], out)
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(results[0].status, ExtractionStatus.SKIPPED)

    def test_dry_run_no_files_written(self) -> None:
        """Just parse, don't write anything."""
        with _TempDir() as tmp:
            p = tmp / "test.ipsw"
            _make_minimal_ipsw(p)
            info = parse_ipsw(p)
            # dry-run = don't call extract_ramdisk — just verify info
            self.assertIsNotNone(info)
            self.assertTrue(info.output_filename)


# ══════════════════════════════════════════════════════════════════════════
#  Tests: CLI entry point (smoke)
# ══════════════════════════════════════════════════════════════════════════

class TestCLI(unittest.TestCase):
    """Command-line interface smoke tests."""

    def test_help_exits_0(self) -> None:
        result = subprocess.run(
            [sys.executable, "backend.py", "--help"],
            capture_output=True, text=True, cwd=_HERE,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout.lower())

    def test_verbose_dry_run_on_empty_dir(self) -> None:
        with _TempDir() as tmp:
            result = subprocess.run(
                [sys.executable, "backend.py", str(tmp), "--dry-run", "-v"],
                capture_output=True, text=True, cwd=_HERE,
            )
            # dry-run on empty dir → exit 0, no IPSWs message
            self.assertIn("No .ipsw files", result.stdout + result.stderr)

    def test_verbose_dry_run_on_populated_dir(self) -> None:
        with _TempDir() as tmp:
            p = tmp / "test.ipsw"
            _make_minimal_ipsw(p)
            result = subprocess.run(
                [sys.executable, "backend.py", str(tmp), "--dry-run", "-v"],
                capture_output=True, text=True, cwd=_HERE,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("iPhone12,1", result.stdout)


# ══════════════════════════════════════════════════════════════════════════
#  Test loader
# ══════════════════════════════════════════════════════════════════════════

def load_tests() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        TestModels,
        TestBackendScan,
        TestBackendParse,
        TestBackendValidateDMG,
        TestUniquePath,
        TestBackendExtract,
        TestBackendBatch,
        TestCLI,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return suite


# ══════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_coverage = "--coverage" in sys.argv
    if run_coverage:
        sys.argv.remove("--coverage")

    suite = load_tests()
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)

    if run_coverage:
        try:
            import coverage
            cov = coverage.Coverage(source=["models", "backend"])
            cov.start()
            # Re-import to measure coverage
            import importlib
            for mod in ("models", "backend"):
                importlib.reload(sys.modules.get(mod))
            cov.stop()
            cov.report()
        except ImportError:
            print("\n[coverage] not installed — skipping coverage report")
            print("[coverage] install with: pip install coverage")

    sys.exit(0 if result.wasSuccessful() else 1)
