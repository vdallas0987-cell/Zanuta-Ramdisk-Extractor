"""
Data models for the Ramdisk Extractor.

Defines the core data structures used throughout the application:
  - IPSWInfo: metadata parsed from an IPSW's BuildManifest.plist
  - ExtractionResult: outcome of extracting a single ramdisk
  - Stats: aggregated counters for a batch operation
  - ExtractionStatus: state machine for the extraction lifecycle
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional, List


class ExtractionStatus(Enum):
    """State machine for per-IPSW processing lifecycle."""
    PENDING = auto()
    SCANNING = auto()
    READY = auto()
    EXTRACTING = auto()
    SUCCESS = auto()
    ERROR = auto()
    SKIPPED = auto()


# Known A12/A13 iPhone models (immutable set)
A12_A13_DEVICES = frozenset({
    "iPhone11,2",   # XS
    "iPhone11,4",   # XS Max (China)
    "iPhone11,6",   # XS Max
    "iPhone11,8",   # XR
    "iPhone12,1",   # 11
    "iPhone12,3",   # 11 Pro
    "iPhone12,5",   # 11 Pro Max
    "iPhone12,8",   # SE 2nd gen
})

# ── Required firmware components (extracted alongside ramdisk) ────────────
# ── Required firmware components (extracted alongside ramdisk) ────────────
# Prefer "RestoreSEP" over "SEP" (both map to SEPFirmware.img4)
REQUIRED_COMPONENTS: dict[str, str] = {
    "iBSS":        "iBSS.img4",
    "iBEC":        "iBEC.img4",
    "DeviceTree":  "DeviceTree.img4",
    "KernelCache": "KernelCache.img4",
    "RestoreSEP":  "SEPFirmware.img4",   # preferred (restore-specific)
    "SEP":         "SEPFirmware.img4",   # fallback
}


TOOL_VERSION: str = "1.2"


DEVICE_NAMES: dict[str, str] = {
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max (China)",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd gen)",
}


def is_a12_a13(product_type: str) -> bool:
    """Check whether *product_type* belongs to an A12/A13 iPhone."""
    return product_type in A12_A13_DEVICES


@dataclass
class IPSWInfo:
    """Metadata parsed from a single IPSW's BuildManifest.plist."""

    ipsw_path: Path
    product_type: str
    product_version: str
    product_build: str
    ramdisk_path: str          # path *inside* the ZIP archive
    device_name: str = ""      # populated automatically on first access
    digest: Optional[bytes] = None   # SHA-384 from BuildManifest.Digest (48 bytes)

    def __post_init__(self) -> None:
        if not self.device_name:
            self.device_name = DEVICE_NAMES.get(self.product_type, self.product_type)

    @property
    def display_name(self) -> str:
        """Human-readable label, e.g. 'iPhone 11 (iPhone12,1)'."""
        return f"{self.device_name} ({self.product_type})"

    @property
    def output_filename(self) -> str:
        """Filename for the extracted ramdisk, e.g. iPhone12,1_14.3_18C66_ramdisk.dmg."""
        return f"{self.product_type}_{self.product_version}_{self.product_build}_ramdisk.dmg"

    @property
    def output_relative_path(self) -> Path:
        """
        Relative path under the output base directory, e.g.::

            iPhone12,1/14.3/iPhone12,1_14.3_18C66_ramdisk.dmg
        """
        return Path(self.product_type) / self.product_version / self.output_filename


@dataclass
class ExtractionResult:
    """Outcome of attempting to extract a single ramdisk."""

    ipsw_info: IPSWInfo
    status: ExtractionStatus
    message: str = ""
    output_path: Optional[Path] = None
    inspection: Optional[DMGInspection] = None


@dataclass
class Stats:
    """Aggregate counters for a batch of IPSW processing."""

    total: int = 0
    success: int = 0
    skipped: int = 0
    error: int = 0

    @property
    def completed(self) -> int:
        return self.success + self.skipped + self.error

    def __str__(self) -> str:
        return (
            f"Total: {self.total}  |  "
            f"Ok: {self.success}  |  "
            f"Skipped: {self.skipped}  |  "
            f"Errors: {self.error}"
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "success": self.success,
            "skipped": self.skipped,
            "error": self.error,
        }


@dataclass
class DMGInspection:
    """Result of verifying a DMG file's structure."""

    format_name: str           # human-readable format, e.g. "UDIF DMG"
    file_size: int             # bytes on disk
    structure_valid: bool      # structural integrity check passed
    container_size: Optional[int] = None  # declared size by the container
    filesystem: Optional[str] = None      # e.g. "APFS", "HFS+", "HFSX"
    details: str = ""          # human-readable summary
    digest_verified: Optional[bool] = None  # None = not checked, True = match, False = mismatch
    encrypted: Optional[bool] = None        # None = unknown, True/False


# ── All-components extraction ──────────────────────────────────────────


@dataclass
class ComponentInfo:
    """Metadata for a single firmware component inside an IPSW."""
    name: str                    # Manifest key, e.g. "KernelCache", "iBEC"
    path_in_zip: str             # path inside the ZIP archive
    product_type: str            # e.g. "iPhone11,8"
    product_version: str         # e.g. "18.7.9"
    product_build: str           # e.g. "22H355"
    source_ipsw: Path            # path to the source IPSW
    is_firmware_payload: bool = False       # marked as IsFirmwarePayload
    is_secondary_payload: bool = False      # marked as IsSecondaryFirmwarePayload
    digest: Optional[bytes] = None          # SHA-384 from BuildManifest.Digest (48 bytes)


@dataclass
class ComponentResult:
    """Outcome of extracting a single firmware component."""
    component: ComponentInfo
    status: ExtractionStatus
    message: str = ""
    output_path: Optional[Path] = None
    digest_verified: Optional[bool] = None
