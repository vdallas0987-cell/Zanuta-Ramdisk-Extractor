"""
Tests for models.py — data classes, enums, constants.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from models import (
    A12_A13_DEVICES,
    DEVICE_NAMES,
    DMGInspection,
    ExtractionResult,
    ExtractionStatus,
    IPSWInfo,
    Stats,
    is_a12_a13,
)


class TestExtractionStatus(unittest.TestCase):
    def test_members(self) -> None:
        self.assertTrue(ExtractionStatus.PENDING)
        self.assertTrue(ExtractionStatus.SCANNING)
        self.assertTrue(ExtractionStatus.READY)
        self.assertTrue(ExtractionStatus.EXTRACTING)
        self.assertTrue(ExtractionStatus.SUCCESS)
        self.assertTrue(ExtractionStatus.ERROR)
        self.assertTrue(ExtractionStatus.SKIPPED)

    def test_enum_values_are_unique(self) -> None:
        values = [s.value for s in ExtractionStatus]
        self.assertEqual(len(values), len(set(values)))


class TestDeviceIdentification(unittest.TestCase):
    def test_all_devices_in_device_names(self) -> None:
        for device in A12_A13_DEVICES:
            self.assertIn(device, DEVICE_NAMES)

    def test_all_device_names_have_a12_a13(self) -> None:
        for device in DEVICE_NAMES:
            self.assertIn(device, A12_A13_DEVICES)

    def test_is_a12_a13_positive(self) -> None:
        for device in A12_A13_DEVICES:
            self.assertTrue(is_a12_a13(device))

    def test_is_a12_a13_negative(self) -> None:
        self.assertFalse(is_a12_a13("iPhone13,1"))  # A14
        self.assertFalse(is_a12_a13("iPhone14,2"))  # A15
        self.assertFalse(is_a12_a13("iPad8,1"))
        self.assertFalse(is_a12_a13("unknown"))


class TestIPSWInfo(unittest.TestCase):
    def test_display_name_known(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/fakepath/ipsw.ipsw"),
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )
        self.assertIn("iPhone XR", info.display_name)
        self.assertIn("iPhone11,8", info.display_name)

    def test_display_name_unknown(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/fakepath/ipsw.ipsw"),
            product_type="iPhone99,9",
            product_version="1.0",
            product_build="1A1",
            ramdisk_path="ramdisk.dmg",
        )
        self.assertIn("iPhone99,9", info.display_name)

    def test_output_filename_format(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/fakepath/ipsw.ipsw"),
            product_type="iPhone11,8",
            product_version="18.7.9",
            product_build="22H355",
            ramdisk_path="094-32147-023.dmg",
        )
        self.assertEqual(info.output_filename, "iPhone11,8_18.7.9_22H355_ramdisk.dmg")

    def test_output_relative_path_format(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/fakepath/ipsw.ipsw"),
            product_type="iPhone12,1",
            product_version="14.3",
            product_build="18C66",
            ramdisk_path="ramdisk.dmg",
        )
        expected = Path("iPhone12,1") / "14.3" / "iPhone12,1_14.3_18C66_ramdisk.dmg"
        self.assertEqual(info.output_relative_path, expected)

    def test_device_name_auto_fills(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/x.ipsw"),
            product_type="iPhone11,8",
            product_version="1.0",
            product_build="1A1",
            ramdisk_path="r.dmg",
        )
        self.assertEqual(info.device_name, "iPhone XR")

    def test_device_name_can_be_overridden(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("/x.ipsw"),
            product_type="iPhone11,8",
            product_version="1.0",
            product_build="1A1",
            ramdisk_path="r.dmg",
            device_name="Custom",
        )
        self.assertEqual(info.device_name, "Custom")


class TestExtractionResult(unittest.TestCase):
    def test_minimal_init(self) -> None:
        info = IPSWInfo(
            ipsw_path=Path("t.ipsw"),
            product_type="iPhone11,8", product_version="1.0",
            product_build="1A1", ramdisk_path="r.dmg",
        )
        result = ExtractionResult(ipsw_info=info, status=ExtractionStatus.SUCCESS)
        self.assertEqual(result.status, ExtractionStatus.SUCCESS)
        self.assertEqual(result.message, "")
        self.assertIsNone(result.output_path)
        self.assertIsNone(result.inspection)


class TestStats(unittest.TestCase):
    def test_completed(self) -> None:
        s = Stats(total=10, success=4, skipped=3, error=2)
        self.assertEqual(s.completed, 9)

    def test_completed_default(self) -> None:
        self.assertEqual(Stats().completed, 0)

    def test_str_representation(self) -> None:
        s = Stats(total=5, success=3, skipped=1, error=1)
        text = str(s)
        self.assertIn("Total: 5", text)
        self.assertIn("Ok: 3", text)

    def test_to_dict(self) -> None:
        s = Stats(total=10, success=5, skipped=3, error=2)
        self.assertEqual(s.to_dict(), {"total": 10, "success": 5, "skipped": 3, "error": 2})


class TestDMGInspection(unittest.TestCase):
    def test_minimal(self) -> None:
        insp = DMGInspection(format_name="UDIF DMG", file_size=1_048_576, structure_valid=True)
        self.assertEqual(insp.format_name, "UDIF DMG")
        self.assertEqual(insp.file_size, 1_048_576)
        self.assertTrue(insp.structure_valid)

    def test_invalid_structure(self) -> None:
        insp = DMGInspection(format_name="Unknown", file_size=99, structure_valid=False)
        self.assertFalse(insp.structure_valid)


if __name__ == "__main__":
    unittest.main()
