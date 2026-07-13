"""
Tests for extract_all_components() with unittest.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend import extract_all_components, _find_all_components
from models import ExtractionStatus

TEST_FIXTURES = Path(__file__).parent / "fixtures"
BUILD_MANIFEST_PATH = TEST_FIXTURES / "BuildManifest.plist"

FAKE_CONTENT = b"\x00" * 2048


class TestFindAllComponents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_findcomp_"))
        self.ipsw_path = self.tmp / "iPhone11,8_full.ipsw"
        self._build_fake_ipsw()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_fake_ipsw(self) -> None:
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            for p in [
                "094-32147-023.dmg",
                "kernelcache.release.iphone11b",
                "Firmware/dfu/iBEC.n841.RELEASE.im4p",
                "Firmware/dfu/iBSS.n841.RELEASE.im4p",
                "Firmware/all_flash/sep-firmware.n841.RELEASE.im4p",
                "Firmware/all_flash/DeviceTree.n841ap.im4p",
                "Firmware/all_flash/LLB.n841.RELEASE.im4p",
                "Firmware/all_flash/iBoot.n841.RELEASE.im4p",
                "Firmware/all_flash/applelogo@1792~iphone.im4p",
            ]:
                zf.writestr(p, FAKE_CONTENT)

    def test_finds_all_components(self) -> None:
        comps = _find_all_components(self.ipsw_path)
        self.assertGreater(len(comps), 0)
        keys = [c[0] for c in comps]
        self.assertIn("RestoreRamDisk", keys)
        self.assertIn("KernelCache", keys)
        self.assertIn("iBEC", keys)

    def test_each_component_has_valid_path(self) -> None:
        all_names = []
        with zipfile.ZipFile(self.ipsw_path, "r") as zf:
            all_names = zf.namelist()
        comps = _find_all_components(self.ipsw_path)
        # Now returns 4-tuple: (key, zip_path, info, digest)
        for _, zip_path, _, _ in comps:
            self.assertIn(zip_path, all_names,
                          f"Path '{zip_path}' not in ZIP")

    def test_components_include_digest(self) -> None:
        """Components from the real fixture should have SHA-384 digests."""
        comps = _find_all_components(self.ipsw_path)
        digest_found = 0
        for _, _, _, d in comps:
            if d is not None:
                digest_found += 1
                self.assertEqual(len(d), 48, "SHA-384 must be 48 bytes")
        self.assertGreater(
            digest_found, 0,
            "Expected at least one component with a Digest",
        )


class TestExtractAllComponents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_allcomp_"))
        self.ipsw_path = self.tmp / "iPhone11,8_full.ipsw"
        self._build_fake_ipsw()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_fake_ipsw(self) -> None:
        with zipfile.ZipFile(self.ipsw_path, "w") as zf:
            zf.write(BUILD_MANIFEST_PATH, "BuildManifest.plist")
            for p in [
                "094-32147-023.dmg",
                "kernelcache.release.iphone11b",
                "Firmware/dfu/iBEC.n841.RELEASE.im4p",
                "Firmware/dfu/iBSS.n841.RELEASE.im4p",
                "Firmware/all_flash/sep-firmware.n841.RELEASE.im4p",
                "Firmware/all_flash/DeviceTree.n841ap.im4p",
                "Firmware/all_flash/LLB.n841.RELEASE.im4p",
                "Firmware/all_flash/iBoot.n841.RELEASE.im4p",
                "Firmware/all_flash/applelogo@1792~iphone.im4p",
            ]:
                zf.writestr(p, FAKE_CONTENT)

    def test_extract_all_returns_results(self) -> None:
        results, stats = extract_all_components(self.ipsw_path, self.tmp / "out")
        self.assertGreater(len(results), 0)

    def test_all_succeed(self) -> None:
        results, stats = extract_all_components(self.ipsw_path, self.tmp / "out")
        self.assertEqual(stats.success, stats.total)

    def test_output_structure(self) -> None:
        results, _ = extract_all_components(self.ipsw_path, self.tmp / "out")
        for result in results:
            self.assertTrue(result.output_path.exists())
            json_path = result.output_path.with_suffix(".json")
            self.assertTrue(json_path.exists())

    def test_output_tree_layout(self) -> None:
        results, _ = extract_all_components(self.ipsw_path, self.tmp / "out")
        out_dir = self.tmp / "out"
        self.assertTrue((out_dir / "iPhone11,8" / "18.7.9" / "components").exists())

    def test_with_callbacks(self) -> None:
        items = []
        progress_vals = []
        results, stats = extract_all_components(
            self.ipsw_path, self.tmp / "out",
            item_callback=lambda r: items.append(r),
            progress_callback=lambda c, t: progress_vals.append((c, t)),
        )
        self.assertEqual(len(items), stats.total)
        self.assertEqual(progress_vals[-1], (stats.total, stats.total))

    def test_corrupt_ipsw_returns_empty(self) -> None:
        corrupt = self.tmp / "corrupt.ipsw"
        corrupt.write_text("not a zip")
        results, stats = extract_all_components(corrupt, self.tmp / "out")
        self.assertEqual(len(results), 0)
        self.assertEqual(stats.error, 1)

    def test_metadata_includes_digest_info(self) -> None:
        """Metadata JSON should include digest information when available."""
        results, _ = extract_all_components(self.ipsw_path, self.tmp / "out")
        digest_in_meta = 0
        for result in results:
            json_path = result.output_path.with_suffix(".json")
            meta = json.loads(json_path.read_text())
            if "digest" in meta:
                digest_in_meta += 1
                self.assertIn("expected_sha384", meta["digest"])
                self.assertIn("verified", meta["digest"])
        self.assertGreater(digest_in_meta, 0,
                           "Expected metadata with digest info")


class TestDigestVerification(unittest.TestCase):
    """Unit tests for SHA-384 digest verification logic."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ramdisk_test_digest_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verify_matching_digest(self) -> None:
        from backend import _verify_digest, _compute_sha384
        content = b"hello world" * 1000
        p = self.tmp / "test.bin"
        p.write_bytes(content)
        expected = _compute_sha384(p)
        self.assertTrue(_verify_digest(p, expected))

    def test_verify_mismatching_digest(self) -> None:
        from backend import _verify_digest
        content = b"hello world" * 1000
        p = self.tmp / "test.bin"
        p.write_bytes(content)
        wrong_digest = b"\x00" * 48
        self.assertFalse(_verify_digest(p, wrong_digest))

    def test_verify_none_digest_returns_true(self) -> None:
        from backend import _verify_digest
        p = self.tmp / "test.bin"
        p.write_bytes(b"anything")
        self.assertTrue(_verify_digest(p, None))

    def test_compute_sha384_returns_48_bytes(self) -> None:
        from backend import _compute_sha384
        p = self.tmp / "test.bin"
        p.write_bytes(b"test data" * 500)
        d = _compute_sha384(p)
        self.assertEqual(len(d), 48)

    def test_compute_sha384_deterministic(self) -> None:
        from backend import _compute_sha384
        p1 = self.tmp / "a.bin"
        p2 = self.tmp / "b.bin"
        p1.write_bytes(b"same content" * 300)
        p2.write_bytes(b"same content" * 300)
        self.assertEqual(_compute_sha384(p1), _compute_sha384(p2))


if __name__ == "__main__":
    unittest.main()
