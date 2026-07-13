"""
DMG validation and structural inspection for Apple disk images.
"""

from __future__ import annotations

import logging
import sys
import struct
import subprocess
from pathlib import Path
from typing import Callable, Optional

from models import DMGInspection

logger = logging.getLogger(__name__)

# Each check is a tuple: (format_name, magic_bytes, offset_or_callable)
# where offset_or_callable can be:
#   int           — absolute offset from start
#   callable(int) — receives file_size and returns offset
_DMG_SIGNATURES: list[tuple[str, bytes, int | Callable[[int], int]]] = [
    ("UDIF DMG",          b"koly", lambda fs: fs - 512),
    ("APFS container",    b"NXSB", 0),
    ("APFS container",    b"NXSB", 1024),
    ("HFS+",              b"H+",   1024),
    ("HFSX (Extended)",   b"HX",   1024),
    ("GPT header",        b"EFI PART", 512),
    ("IM4P (Image4)",     b"IM4P", 0),
    ("IM4C (Image4)",     b"IM4C", 0),
]

_MIN_DMG_SIZE = 1_048_576     # 1 MB — ramdisks are always larger
CHUNK_SIZE = 256 * 1024      # 256 KB streaming buffer


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

def _read_uint32(data: bytes, offset: int) -> int:
    """Read a little-endian uint32 from *data* at *offset*."""
    return struct.unpack_from("<I", data, offset)[0]


def _read_uint64(data: bytes, offset: int) -> int:
    """Read a little-endian uint64 from *data* at *offset*."""
    return struct.unpack_from("<Q", data, offset)[0]


# ---------------------------------------------------------------------------
#  Signature detection
# ---------------------------------------------------------------------------

def _check_signatures(dmg_path: Path, file_size: int) -> Optional[str]:
    """Try to detect the image format by checking known magic bytes.

    Returns the format name if recognised, or ``None``.
    """
    with open(dmg_path, "rb") as fh:
        for name, magic, offset_spec in _DMG_SIGNATURES:
            offset = offset_spec(file_size) if callable(offset_spec) else offset_spec
            if offset < 0 or offset + len(magic) > file_size:
                continue
            fh.seek(offset)
            data = fh.read(len(magic))
            if data == magic:
                return name
    return None


# ---------------------------------------------------------------------------
#  Format-specific inspectors
# ---------------------------------------------------------------------------

def _inspect_udif(fh, file_size: int) -> DMGInspection:
    """Parse the UDIF koly trailer for structural info."""
    fh.seek(file_size - 512)
    trailer = fh.read(512)
    version = _read_uint32(trailer, 4)
    header_size = _read_uint32(trailer, 8)
    flags = _read_uint32(trailer, 12)    # bitmask: bit 3 = kUDIFEncrypted (0x08)
    is_encrypted = bool(flags & 0x08)
    valid = version <= 4 and 512 <= header_size <= 4096

    detail_parts = [
        f"koly v{version}, {header_size}-byte trailer, "
        f"{file_size // (1024*1024)} MB on disk",
    ]
    if is_encrypted:
        detail_parts.append("ENCRYPTED (requires decryption keys)")
    if not valid:
        detail_parts.append("WARNING: malformed koly trailer")

    return DMGInspection(
        format_name="UDIF DMG",
        file_size=file_size,
        structure_valid=valid,
        container_size=file_size,
        filesystem="Unknown (UDIF container)",
        encrypted=is_encrypted,
        details=" | ".join(detail_parts),
    )


def _inspect_apfs(fh, file_size: int) -> DMGInspection:
    """Parse the APFS NXSB superblock for structural info."""
    fh.seek(0)
    block = fh.read(64)
    block_size = _read_uint32(block, 4)
    total_blocks = _read_uint64(block, 8)
    valid = (
        block_size >= 4096
        and block_size <= 65536
        and (block_size & (block_size - 1)) == 0
        and total_blocks > 0
    )
    container_size = block_size * total_blocks if valid else None
    truncated = valid and file_size < container_size
    if truncated:
        valid = False
        detail = (
            f"APFS: declared {container_size // (1024*1024)} MB, "
            f"but file has {file_size // (1024*1024)} MB — TRUNCATED"
        )
    elif valid:
        detail = (
            f"APFS: {total_blocks} blocks x {block_size // 1024} KB = "
            f"{container_size // (1024*1024)} MB, "
            f"on disk {file_size // (1024*1024)} MB"
        )
    else:
        detail = "APFS: malformed superblock"
    return DMGInspection(
        format_name="APFS container",
        file_size=file_size,
        structure_valid=valid,
        container_size=container_size,
        filesystem="APFS",
        details=detail,
    )


def _inspect_hfs(fh, file_size: int, signature: str) -> DMGInspection:
    """Parse the HFS+ / HFSX volume header at offset 1024."""
    fh.seek(1024)
    block = fh.read(512)
    block_size = _read_uint32(block, 40)
    total_blocks = _read_uint32(block, 44)
    valid = block_size >= 512 and total_blocks > 0
    container_size = block_size * total_blocks if valid else None
    detail = (
        f"{signature}: {total_blocks} blocks x {block_size // 1024} KB = "
        f"{container_size // (1024*1024)} MB"
    ) if valid else f"{signature}: malformed volume header"
    return DMGInspection(
        format_name=signature,
        file_size=file_size,
        structure_valid=valid,
        container_size=container_size,
        filesystem=signature,
        details=detail,
    )


def _inspect_gpt(fh, file_size: int) -> DMGInspection:
    """Parse the GPT header at offset 512 for structural info."""
    fh.seek(512)
    header = fh.read(92)
    revision = _read_uint32(header, 4)
    header_size = _read_uint32(header, 8)
    num_partitions = _read_uint32(header, 80)

    valid = (
        revision in (0x00010000,)  # GPT revision 1.0
        and header_size >= 92
        and num_partitions > 0
    )
    detail = (
        f"GPT v{revision:x}, {num_partitions} partition(s), "
        f"{file_size // (1024*1024)} MB on disk"
    ) if valid else "GPT: malformed header"
    return DMGInspection(
        format_name="GPT disk image",
        file_size=file_size,
        structure_valid=valid,
        container_size=file_size,
        filesystem="GPT",
        details=detail,
    )


def _try_hdiutil(dmg_path: Path) -> Optional[str]:
    """Try ``hdiutil verify`` on macOS for native DMG verification.

    Only called when ``sys.platform == "darwin"``.
    """
    try:
        result = subprocess.run(
            ["hdiutil", "verify", str(dmg_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return "hdiutil: OK"
        return f"hdiutil: {result.stdout.strip() or result.stderr.strip()}"
    except FileNotFoundError:
        return None
    except subprocess.SubprocessError as exc:
        return f"hdiutil error: {exc}"


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def validate_dmg(dmg_path: Path) -> bool:
    """Check whether *dmg_path* appears to be a valid Apple disk image.

    Uses an extensible set of magic-byte checks to recognise:
      * UDIF DMG (``koly``)
      * APFS container (``NXSB``)
      * HFS+ / HFSX (``H+`` / ``HX``)
      * GPT disk image (``EFI PART``)
      * ``file(1)`` command as a final fallback

    If nothing is recognised the file is **kept** with a warning — an
    unrecognised format does not mean the image is corrupt.
    """
    dmg_path = Path(dmg_path)
    if not dmg_path.is_file():
        return False

    file_size = dmg_path.stat().st_size
    if file_size < _MIN_DMG_SIZE:
        logger.warning(
            "Rejected: %s too small (%d bytes)", dmg_path.name, file_size,
        )
        return False

    # ── Magic-byte signature checks ────────────────────────────────
    try:
        fmt = _check_signatures(dmg_path, file_size)
        if fmt is not None:
            logger.debug("Detected %s: %s", fmt, dmg_path.name)
            return True
    except (IOError, OSError) as exc:
        logger.warning("I/O error reading image signatures: %s", exc)

    # ── file(1) fallback ───────────────────────────────────────────
    try:
        result = subprocess.run(
            ["file", "-b", str(dmg_path)],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.lower()
        keywords = (
            "apple disk image", "apple udrw", "apple udif",
            "zlib compressed data", "bzip2 compressed data",
            "lzfse compressed data", "apple filesystem", "disk image",
        )
        if any(kw in output for kw in keywords) or " dmg" in output:
            logger.debug("Detected via file(1): %s", dmg_path.name)
            return True
    except FileNotFoundError:
        logger.debug("file(1) not available — skipping fallback")
    except subprocess.SubprocessError as exc:
        logger.warning("file(1) fallback failed: %s", exc)

    # ── Unrecognised ───────────────────────────────────────────────
    logger.warning(
        "Unknown image format: %s (no recognised signature found) — "
        "file kept, but may not be a standard DMG",
        dmg_path.name,
    )
    return True


def inspect_dmg(dmg_path: Path, fast: bool = True) -> DMGInspection:
    """Analyse a DMG file and return a structural inspection report.

    Checks performed:
      * Magic-byte format detection (UDIF / APFS / HFS+ / GPT).
      * Container header consistency (block size, total blocks).
      * File-size vs declared-size comparison.
      * ``hdiutil verify`` on macOS (runs automatically on macOS).

    Parameters
    ----------
    dmg_path
        Path to the extracted DMG file.
    fast
        Deprecated and ignored. ``hdiutil`` verification is now gated
        on ``sys.platform == 'darwin'`` automatically.

    Returns
    -------
    DMGInspection
    """
    dmg_path = Path(dmg_path)
    if not dmg_path.is_file():
        return DMGInspection(
            format_name="N/A", file_size=0,
            structure_valid=False, details="File not found",
        )

    file_size = dmg_path.stat().st_size

    try:
        fmt_name = _check_signatures(dmg_path, file_size)
    except (IOError, OSError) as exc:
        return DMGInspection(
            format_name="Error", file_size=file_size,
            structure_valid=False, details=f"I/O error: {exc}",
        )

    # ── Format-specific deeper inspection ─────────────────────────
    with open(dmg_path, "rb") as fh:
        if fmt_name == "UDIF DMG":
            result = _inspect_udif(fh, file_size)
        elif fmt_name and "APFS" in fmt_name:
            result = _inspect_apfs(fh, file_size)
        elif fmt_name in ("HFS+", "HFSX (Extended)"):
            result = _inspect_hfs(fh, file_size, fmt_name)
        elif fmt_name == "GPT header":
            result = _inspect_gpt(fh, file_size)
        else:
            fh.seek(0)
            head = fh.read(16)
            hex_sig = head.hex(" ")[:47].upper() if head else "(empty)"
            ascii_sig = (
                "".join(chr(b) if 32 <= b < 127 else "." for b in head)
                if head else ""
            )
            result = DMGInspection(
                format_name=fmt_name or "Unknown",
                file_size=file_size,
                structure_valid=False,
                container_size=file_size,
                details=(
                    f"{file_size // (1024*1024)} MB — "
                    f"unrecognised format, bytes: {hex_sig}  ({ascii_sig})"
                ),
            )

    # ── macOS hdiutil (runs when sys.platform == "darwin") ────────────
    if sys.platform == "darwin":
        hdiutil_msg = _try_hdiutil(dmg_path)
        if hdiutil_msg:
            result.details += f" | {hdiutil_msg}"

    return result


# ---------------------------------------------------------------------------
#  Firmware component validation (magic bytes)
# ---------------------------------------------------------------------------

# Magic bytes for common firmware component formats
_COMPONENT_MAGIC: dict[str, bytes] = {
    "IM4P": b"IM4P",        # iBSS, iBEC, DeviceTree, SEP
    "IM4C": b"IM4C",        # Alternative Image4 container
    "MACHO_32": b"\xFE\xED\xFACE",    # 32-bit Mach-O (kernelcache)
    "MACHO_64": b"\xFE\xED\xFA\xCF",  # 64-bit Mach-O (kernelcache)
    "MACHO_FAT": b"\xCA\xFE\xBA\xBE", # Fat binary (kernelcache)
    "MACHO_64_BE": b"\xCF\xFA\xED\xFE", # 64-bit little-endian Mach-O
}

# Which components are expected to be IM4P format
_IM4P_COMPONENTS = frozenset({"iBSS", "iBEC", "DeviceTree", "RestoreSEP", "SEP"})

# KernelCache may be Mach-O or IM4P (or raw)
_KERNELCACHE_KEYS = frozenset({"KernelCache", "RestoreKernelCache"})


def _component_output_name(manifest_key: str) -> str:
    """Return a short, stable name for logging."""
    return {
        "iBSS": "iBSS",
        "iBEC": "iBEC",
        "DeviceTree": "DeviceTree",
        "RestoreDeviceTree": "DeviceTree",
        "KernelCache": "KernelCache",
        "RestoreKernelCache": "KernelCache",
        "SEP": "SEP",
        "RestoreSEP": "SEP",
    }.get(manifest_key, manifest_key)


def validate_component(path: Path, manifest_key: str) -> Optional[str]:
    """Validate *path* by checking expected magic bytes for *manifest_key*.

    Returns a warning string if validation is inconclusive, or ``None``
    if the file looks valid (or no validation is defined for this key).
    """
    if not path.is_file():
        return f"File not found: {path}"

    file_size = path.stat().st_size
    if file_size == 0:
        return f"Empty file: {path.name}"

    with open(path, "rb") as fh:
        head = fh.read(8)  # read enough for any magic

    if not head:
        return f"Unreadable: {path.name}"

    # IM4P check
    if manifest_key in _IM4P_COMPONENTS:
        if head[:4] == _COMPONENT_MAGIC["IM4P"]:
            return None  # valid
        return (
            f"Expected IM4P header but got {head[:4].hex()} "
            f"({_component_output_name(manifest_key)})"
        )

    # KernelCache: can be Mach-O, IM4P, or raw
    if manifest_key in _KERNELCACHE_KEYS:
        if head[:4] == _COMPONENT_MAGIC["IM4P"]:
            return None
        for name in ("MACHO_32", "MACHO_64", "MACHO_FAT", "MACHO_64_BE"):
            if head[:4] == _COMPONENT_MAGIC[name]:
                return None
        # Unknown — log but don't reject (kernelcache may be uncompressed)
        return (
            f"Unrecognised header {head[:4].hex()} for kernelcache "
            f"— file kept, may be a custom format"
        )

    return None  # no validation defined
