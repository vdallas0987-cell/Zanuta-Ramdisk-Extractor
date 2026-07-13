"""
Tests for ramdisk extraction with unittest.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import extract_ramdisk, extract_required_components, _unique_path, process_all, save_unified_metadata
from models import ExtractionStatus, IPSWInfo

TEST_FIXTURES = Path(__file__).parent / "fixtures"
BUILD_MANIFEST_PATH = TEST_FIXTURES / "BuildManifest.plist"


class TestUniquePath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_unique_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_existent_unchanged(self) -> None:
        p = self.tmp / "test.dmg"
        self.assertEqual(_unique_path(p), p)

    def test_existing_gets_suffix(self) -> None:
        p = self.tmp / "test.dmg"
        p.write_bytes(b"content")
        unique = _unique_path(p)
        self.assertNotEqual(unique, p)
        self.assertEqual(unique.stem, "test_1")

    def test_multiple_collisions(self) -> None:
        for name in ("test.dmg", "test_1.dmg", "test_2.dmg"):
            (self.tmp / name).write_bytes(b"")
        unique = _unique_path(self.tmp / "test.dmg")
        self.assertEqual(unique.name, "test_3.dmg")


class TestExtractRamdisk(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_extract_"))
        self.ipsw_path = self.tmp / "fixture.ipsw"
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", b"\x00" * (1024 * 1024))
        self.info = IPSWInfo(
            ipsw_path=self.ipsw_path,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_success(self) -> None:
        result = extract_ramdisk(self.info, self.tmp)
        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        self.assertIsNotNone(result.output_path)
        self.assertTrue(result.output_path.exists())

    def test_extract_creates_metadata(self) -> None:
        result = extract_ramdisk(self.info, self.tmp)
        json_path = result.output_path.with_suffix(".json")
        self.assertTrue(json_path.exists())
        meta = json.loads(json_path.read_text())
        self.assertEqual(meta["device"]["product_type"], "iPhone11,8")
        self.assertIn("inspection", meta)

    def test_extract_nonexistent_ramdisk_errors(self) -> None:
        info = IPSWInfo(
            ipsw_path=self.ipsw_path,
            product_type="iPhone11,8", product_version="1.0",
            product_build="1A1", ramdisk_path="nonexistent.dmg",
        )
        result = extract_ramdisk(info, self.tmp)
        self.assertEqual(result.status, ExtractionStatus.ERROR)

    def test_progress_callback(self) -> None:
        calls: list[int] = []
        result = extract_ramdisk(
            self.info, self.tmp, progress_callback=lambda p: calls.append(p),
        )
        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        self.assertGreater(len(calls), 0)
        self.assertEqual(calls[-1], 100)

    def test_unique_path_on_duplicate(self) -> None:
        r1 = extract_ramdisk(self.info, self.tmp)
        r2 = extract_ramdisk(self.info, self.tmp)
        self.assertEqual(r1.status, ExtractionStatus.SUCCESS)
        self.assertEqual(r2.status, ExtractionStatus.SUCCESS)
        self.assertNotEqual(r1.output_path, r2.output_path)


class TestProcessAll(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_process_"))
        self.ipsw_path = self.tmp / "fixture.ipsw"
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", b"\x00" * (1024 * 1024))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_process_single(self) -> None:
        results, stats = process_all([self.ipsw_path], self.tmp / "out")
        self.assertEqual(len(results), 1)
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.success, 1)

    def test_process_corrupt_errors(self) -> None:
        corrupt = self.tmp / "corrupt.ipsw"
        corrupt.write_text("not a zip")
        results, stats = process_all([corrupt], self.tmp / "out")
        self.assertEqual(stats.error, 1)

    def test_process_with_callbacks(self) -> None:
        items = []
        results, stats = process_all(
            [self.ipsw_path], self.tmp / "out",
            item_callback=lambda r: items.append(r),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(stats.success, 1)


if __name__ == "__main__":
    unittest.main()


class TestExtractRequiredComponents(unittest.TestCase):
    """Tests for extract_required_components() — iBSS, iBEC, DeviceTree, KernelCache, SEP."""

    FAKE_CONTENT = b"\x00" * 4096

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_reqcomp_"))
        self.ipsw_path = self.tmp / "iPhone11,8_full.ipsw"
        self._build_fake_ipsw()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_fake_ipsw(self) -> None:
        """Create a fake IPSW with BuildManifest + the specific component files."""
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            # Ramdisk
            zf.writestr("094-32147-023.dmg", self.FAKE_CONTENT)
            # The 5 required components (paths match the fixture's BuildManifest)
            zf.writestr("Firmware/dfu/iBSS.n841.RELEASE.im4p", self.FAKE_CONTENT)
            zf.writestr("Firmware/dfu/iBEC.n841.RELEASE.im4p", self.FAKE_CONTENT)
            zf.writestr("Firmware/all_flash/DeviceTree.n841ap.im4p", self.FAKE_CONTENT)
            zf.writestr("kernelcache.release.iphone11b", self.FAKE_CONTENT)
            zf.writestr("Firmware/all_flash/sep-firmware.n841.RELEASE.im4p", self.FAKE_CONTENT)

    def test_extracts_all_five_components(self) -> None:
        results, stats = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        self.assertEqual(stats.total, 5)
        self.assertEqual(stats.success, 5)
        self.assertEqual(stats.error, 0)

    def test_output_files_have_correct_names(self) -> None:
        results, stats = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        output_names = {p.name for p in (r.output_path for r in results)}
        expected = {"iBSS.img4", "iBEC.img4", "DeviceTree.img4",
                    "KernelCache.img4", "SEPFirmware.img4"}
        self.assertEqual(output_names, expected)

    def test_output_in_correct_directory(self) -> None:
        results, stats = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        for r in results:
            self.assertTrue(
                r.output_path.parent.samefile(self.tmp / "iPhone11,8" / "18.7.9"),
                f"{r.output_path} not in expected directory",
            )

    def test_per_component_metadata_created(self) -> None:
        results, stats = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        for r in results:
            json_path = r.output_path.with_suffix(".json")
            self.assertTrue(json_path.exists(), f"Missing metadata for {r.output_path.name}")
            meta = json.loads(json_path.read_text())
            self.assertEqual(meta["component"]["name"], r.component.name)

    def test_with_callbacks(self) -> None:
        items: list = []
        progress_vals: list = []
        results, stats = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            item_callback=lambda r: items.append(r),
            progress_callback=lambda c, t: progress_vals.append((c, t)),
        )
        self.assertEqual(len(items), 5)
        self.assertEqual(progress_vals[-1], (5, 5))

    def test_unique_path_on_duplicate(self) -> None:
        """Re-extraction should produce suffixed filenames."""
        r1, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        r2, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )
        for r1_res, r2_res in zip(r1, r2):
            self.assertNotEqual(r1_res.output_path, r2_res.output_path,
                                f"{r1_res.component.name} should have unique path")


class TestSaveUnifiedMetadata(unittest.TestCase):
    """Tests for save_unified_metadata()."""

    FAKE_CONTENT = b"\x00" * 4096

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_unified_"))
        self.ipsw_path = self.tmp / "fixture.ipsw"
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            zf.writestr("094-32147-023.dmg", self.FAKE_CONTENT)
            zf.writestr("Firmware/dfu/iBSS.n841.RELEASE.im4p", self.FAKE_CONTENT)

        self.info = IPSWInfo(
            ipsw_path=self.ipsw_path,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_saves_metadata_json(self) -> None:
        # First extract a ramdisk so we have a real output_path
        ramdisk_result = extract_ramdisk(self.info, self.tmp)
        comp_results, _ = extract_required_components(
            self.ipsw_path, self.tmp,
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
        )

        json_path = save_unified_metadata(
            ipsw_info=self.info,
            ramdisk_output=ramdisk_result.output_path,
            component_results=comp_results,
            output_base=self.tmp,
        )
        self.assertIsNotNone(json_path)
        self.assertTrue(json_path.exists())

        meta = json.loads(json_path.read_text())
        self.assertEqual(meta["device"]["product_type"], "iPhone11,8")
        self.assertEqual(meta["firmware"]["build"], "22H355")
        self.assertIn("ramdisk", meta)
        self.assertIn("components", meta)
        self.assertEqual(len(meta["components"]), 1)
        self.assertEqual(meta["components"][0]["name"], "iBSS")
