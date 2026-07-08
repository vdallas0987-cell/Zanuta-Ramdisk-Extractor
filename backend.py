"""
Backend logic for Ramdisk Extractor.

Responsibilities (all offline, pure Python):
  1. Recursively scan a directory for ``.ipsw`` files.
  2. Open each IPSW as a ZIP and read its ``BuildManifest.plist``.
  3. Parse device metadata, filter for A12/A13 models.
  4. Extract the restore ramdisk (streaming, without inflating the whole ZIP).
  5. Validate the extracted DMG via UDIF ``koly`` magic bytes.
  6. Aggregate statistics for the whole batch.

Every public function accepts and returns plain Python objects (no GUI coupling),
making this module testable from the command line or unit tests.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

try:
    from .models import (
        ExtractionResult,
        ExtractionStatus,
        IPSWInfo,
        Stats,
        is_a12_a13,
    )
except ImportError:
    from models import (
        ExtractionResult,
        ExtractionStatus,
        IPSWInfo,
        Stats,
        is_a12_a13,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_KOLY_MAGIC = b"koly"         # UDIF trailer signature
_MIN_DMG_SIZE = 1_048_576     # 1 MB — ramdisks are always larger
_CHUNK_SIZE = 256 * 1024      # 256 KB streaming buffer


# ---------------------------------------------------------------------------
#  Scanning
# ---------------------------------------------------------------------------

def find_ipsw_files(directory: Path) -> List[Path]:
    """Recursively discover every ``.ipsw`` under *directory*.

    Raises
    ------
    NotADirectoryError
        If *directory* does not exist or is not a folder.
    """
    directory = Path(directory).resolve(strict=False)
    if not directory.is_dir():
        raise NotADirectoryError(f"Directory not found: {directory}")

    files = sorted(directory.rglob("*.ipsw"))
    logger.info("Found %d IPSW file(s) in %s", len(files), directory)
    return files


# ---------------------------------------------------------------------------
#  BuildManifest parsing
# ---------------------------------------------------------------------------

def _extract_plist(zf: zipfile.ZipFile) -> dict:
    """Read ``BuildManifest.plist`` from *zf*, trying common path variations."""
    candidates = ["BuildManifest.plist"]
    # Some IPSWs nest the manifest under a subdirectory.
    candidates.extend(
        n for n in zf.namelist() if n.endswith("/BuildManifest.plist")
    )

    for candidate in dict.fromkeys(candidates):  # deduplicate while preserving order
        try:
            with zf.open(candidate) as fh:
                data: dict = plistlib.load(fh)
            logger.debug("Read BuildManifest.plist from '%s'", candidate)
            return data
        except (KeyError, plistlib.InvalidFileException):
            continue

    raise ValueError("BuildManifest.plist not found inside IPSW")


def _get_nested(d: dict, dotted: str, default=""):
    """Traverse *d* following a dot-separated key path, returning *default* on
    any ``KeyError``, ``IndexError``, or ``TypeError``."""
    try:
        for key in dotted.split("."):
            key = int(key) if key.lstrip("-").isdigit() else key
            d = d[key]
        return d
    except (KeyError, IndexError, TypeError):
        return default


def _verify_ramdisk_in_zip(zf: zipfile.ZipFile, ramdisk_path: str) -> str:
    """Return the canonical path for *ramdisk_path* inside *zf*.

    Some IPSWs store the ramdisk under a different prefix than the
    BuildManifest declares, so we search by suffix as a fallback.
    """
    if ramdisk_path in zf.namelist():
        return ramdisk_path

    # Fallback: match by suffix  (e.g. declared path is missing a leading dir)
    for name in zf.namelist():
        if name.endswith(ramdisk_path):
            logger.debug("Resolved ramdisk path: '%s' -> '%s'", ramdisk_path, name)
            return name

    raise KeyError(f"Ramdisk path '{ramdisk_path}' not found inside IPSW")


def parse_ipsw(ipsw_path: Path) -> IPSWInfo:
    """Open *ipsw_path* and return parsed metadata.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the IPSW is corrupt, missing ``BuildManifest.plist``, or lacks
        the required plist keys.
    zipfile.BadZipFile
        If the file is not a valid ZIP archive.
    """
    ipsw_path = Path(ipsw_path)
    if not ipsw_path.is_file():
        raise FileNotFoundError(f"IPSW not found: {ipsw_path}")

    try:
        with zipfile.ZipFile(ipsw_path, "r") as zf:
            manifest = _extract_plist(zf)

            identity = _get_nested(manifest, "BuildIdentities.0", {})
            if not identity:
                raise ValueError("BuildIdentities[0] missing from BuildManifest")

            product_type = _get_nested(identity, "Info.ProductType")
            if not product_type:
                raise ValueError("ProductType not found in BuildIdentities[0].Info")

            product_version = str(
                manifest.get("ProductVersion", _get_nested(identity, "Info.ProductVersion", "unknown"))
            )
            product_build = str(
                manifest.get("ProductBuildVersion", _get_nested(identity, "Info.ProductBuildVersion", "unknown"))
            )

            ramdisk_declared = _get_nested(
                identity, "Manifest.Restore.RestoreRamDisk.Info.Path"
            )
            if not ramdisk_declared:
                raise ValueError(
                    "RestoreRamDisk path not found in BuildManifest"
                )

            ramdisk_path = _verify_ramdisk_in_zip(zf, ramdisk_declared)

            logger.debug(
                "Parsed %s — %s / %s (%s)  ramdisk=%s",
                ipsw_path.name, product_type, product_version, product_build,
                ramdisk_path,
            )

            return IPSWInfo(
                ipsw_path=ipsw_path,
                product_type=product_type,
                product_version=product_version,
                product_build=product_build,
                ramdisk_path=ramdisk_path,
            )

    except zipfile.BadZipFile as exc:
        raise ValueError(f"Corrupt ZIP (not a valid IPSW): {exc}") from exc


# ---------------------------------------------------------------------------
#  DMG validation
# ---------------------------------------------------------------------------

def validate_dmg(dmg_path: Path) -> bool:
    """Return ``True`` if *dmg_path* looks like a valid Apple Disk Image.

    Two-layer check:
      1. Read the UDIF trailer (last 512 B) and look for ``koly`` at the start
       of the last block.
      2. If that fails (e.g. on exotic formats), fall back to ``file(1)``.
    """
    dmg_path = Path(dmg_path)
    if not dmg_path.is_file():
        return False

    file_size = dmg_path.stat().st_size
    if file_size < _MIN_DMG_SIZE:
        logger.debug("Rejected: %s too small (%d bytes)", dmg_path.name, file_size)
        return False

    # --- Check UDIF trailer ('koly' at offset 0 of the last 512 bytes) ---
    # The UDIF trailer is a fixed 512-byte block at the very end of
    # the file.  The first 4 bytes are the signature 'koly'.
    try:
        with open(dmg_path, "rb") as fh:
            fh.seek(file_size - 512)
            trailer = fh.read(4)
            if trailer == _KOLY_MAGIC:
                logger.debug("Valid DMG via koly trailer: %s", dmg_path.name)
                return True
    except (IOError, OSError) as exc:
        logger.warning("I/O error reading DMG trailer: %s", exc)

    # --- Fallback: file(1) ---
    # The koly check above is sufficient for Apple DMGs.  The file(1)
    # fallback is a best-effort extra; on systems where file(1) is not
    # installed (e.g. Windows) we skip it silently.
    try:
        result = subprocess.run(
            ["file", "-b", str(dmg_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.lower()
        if any(kw in output for kw in (
            "apple disk image", "apple udrw", "dmg",
            "zlib compressed data", "bzip2 compressed data",
        )):
            logger.debug("Valid DMG via file(1): %s", dmg_path.name)
            return True
    except FileNotFoundError:
        logger.debug("file(1) not available — skipping fallback")
    except subprocess.SubprocessError as exc:
        logger.warning("file(1) fallback failed: %s", exc)

    logger.warning("DMG validation FAILED: %s", dmg_path.name)
    return False


# ---------------------------------------------------------------------------
#  Extraction
# ---------------------------------------------------------------------------


def _unique_path(path: Path) -> Path:
    """Return *path* with a ``_N`` suffix inserted before the extension if
    *path* already exists on disk, incrementing N until a free name is found.
    If *path* does not exist, return it unchanged.
    """
    if not path.exists():
        return path

    stem = path.stem          # e.g. "iPhone12,1_18.5_18F5053e_ramdisk"
    suffix = path.suffix      # e.g. ".dmg"
    parent = path.parent

    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def extract_ramdisk(
    ipsw_info: IPSWInfo,
    output_base: Path,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> ExtractionResult:
    """Stream the restore ramdisk from the IPSW archive to disk.

    Parameters
    ----------
    ipsw_info
        Metadata of the IPSW to extract from.
    output_base
        Root directory under which the ``<ProductType>/<Version>/`` tree
        will be created.
    progress_callback
        Optional callable receiving a percentage ``int 0-100``.

    Returns
    -------
    ExtractionResult
        Outcome with the final status and path.
    """
    base_path = output_base / ipsw_info.output_relative_path
    output_path = _unique_path(base_path)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as exc:
        return ExtractionResult(
            ipsw_info=ipsw_info,
            status=ExtractionStatus.ERROR,
            message=f"Cannot create output directory: {exc}",
        )

    try:
        with zipfile.ZipFile(ipsw_info.ipsw_path, "r") as zf:
            info = zf.getinfo(ipsw_info.ramdisk_path)
            total_size = info.file_size or 1  # avoid division by zero

            with zf.open(ipsw_info.ramdisk_path) as src, \
                 open(output_path, "wb") as dst:
                bytes_read = 0
                while True:
                    chunk = src.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    bytes_read += len(chunk)
                    if progress_callback is not None:
                        progress_callback(int(bytes_read * 100 / total_size))

    except (zipfile.BadZipFile, OSError) as exc:
        output_path.unlink(missing_ok=True)
        return ExtractionResult(
            ipsw_info=ipsw_info,
            status=ExtractionStatus.ERROR,
            message=f"Extraction failed: {exc}",
        )

    # Validate the extracted file
    if not validate_dmg(output_path):
        output_path.unlink(missing_ok=True)
        return ExtractionResult(
            ipsw_info=ipsw_info,
            status=ExtractionStatus.ERROR,
            message="Extracted file is not a valid DMG (koly trailer missing)",
        )

    return ExtractionResult(
        ipsw_info=ipsw_info,
        status=ExtractionStatus.SUCCESS,
        message=f"Extracted to {output_path}",
        output_path=output_path,
    )


# ---------------------------------------------------------------------------
#  Batch orchestrator
# ---------------------------------------------------------------------------

def process_all(
    ipsw_paths: List[Path],
    output_base: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    item_callback: Optional[Callable[[ExtractionResult], None]] = None,
) -> Tuple[List[ExtractionResult], Stats]:
    """Process a batch of IPSW files sequentially.

    Parameters
    ----------
    ipsw_paths
        List of paths to ``.ipsw`` files.
    output_base
        Root output directory.
    progress_callback
        ``fn(current_index, total)`` called after each item is processed.
    item_callback
        ``fn(result)`` called after each item, useful for real-time UI updates.

    Returns
    -------
    tuple[list[ExtractionResult], Stats]
    """
    output_base = Path(output_base)
    output_base.mkdir(parents=True, exist_ok=True)

    stats = Stats(total=len(ipsw_paths))
    results: List[ExtractionResult] = []

    for idx, ipsw_path in enumerate(ipsw_paths):
        logger.info("[%d/%d] %s", idx + 1, len(ipsw_paths), ipsw_path.name)

        # --- Parse ---
        try:
            ipsw_info = parse_ipsw(ipsw_path)
        except Exception as exc:
            result = ExtractionResult(
                ipsw_info=IPSWInfo(
                    ipsw_path=ipsw_path,
                    product_type="unknown",
                    product_version="unknown",
                    product_build="unknown",
                    ramdisk_path="",
                ),
                status=ExtractionStatus.ERROR,
                message=str(exc),
            )
            stats.error += 1
            results.append(result)
            _emit(result, item_callback, progress_callback, idx, len(ipsw_paths))
            continue

        # --- Filter ---
        if not is_a12_a13(ipsw_info.product_type):
            result = ExtractionResult(
                ipsw_info=ipsw_info,
                status=ExtractionStatus.SKIPPED,
                message=f"{ipsw_info.product_type} is not an A12/A13 device",
            )
            stats.skipped += 1
            results.append(result)
            _emit(result, item_callback, progress_callback, idx, len(ipsw_paths))
            continue

        # --- Extract ---
        result = extract_ramdisk(ipsw_info, output_base)
        if result.status == ExtractionStatus.SUCCESS:
            stats.success += 1
        else:
            stats.error += 1

        results.append(result)
        _emit(result, item_callback, progress_callback, idx, len(ipsw_paths))

    return results, stats


def _emit(
    result: ExtractionResult,
    item_callback: Optional[Callable[[ExtractionResult], None]],
    progress_callback: Optional[Callable[[int, int], None]],
    idx: int,
    total: int,
) -> None:
    """Convenience: fire both callbacks if they are set."""
    if item_callback is not None:
        item_callback(result)
    if progress_callback is not None:
        progress_callback(idx + 1, total)


# ---------------------------------------------------------------------------
#  CLI entry point (for testing without GUI)
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract restore ramdisks from iPhone A12/A13 IPSWs.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing .ipsw files (scanned recursively)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("./ramdisks"),
        help="Output directory (default: ./ramdisks)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and parse but do not extract anything",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v INFO, -vv DEBUG)",
    )
    args = parser.parse_args()

    # --- Logging ---
    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(levelname).1s %(message)s",
    )

    # --- Scan ---
    try:
        ipsw_files = find_ipsw_files(args.input_dir)
    except NotADirectoryError as exc:
        logger.exception(exc)
        raise SystemExit(1) from exc

    if not ipsw_files:
        logger.warning("No .ipsw files found in %s", args.input_dir)
        raise SystemExit(0)

    # --- Dry-run or process ---
    if args.dry_run:
        print(f"Dry-run: {len(ipsw_files)} IPSW(s) found\n")
        for ipsw_path in ipsw_files:
            try:
                info = parse_ipsw(ipsw_path)
                verdict = "✓ A12/A13" if is_a12_a13(info.product_type) else "✗ skipped"
                print(f"  [{verdict}] {info.display_name:35s}  iOS {info.product_version} ({info.product_build})")
            except Exception as exc:
                print(f"  [ERROR] {ipsw_path.name}: {exc}")
        return

    # --- Process ---
    def _on_item(result: ExtractionResult) -> None:
        icon = {
            ExtractionStatus.SUCCESS: "✓",
            ExtractionStatus.ERROR:   "✗",
            ExtractionStatus.SKIPPED: "–",
        }.get(result.status, "?")
        print(f"  [{icon}] {result.ipsw_info.display_name:35s}  {result.message}")

    def _on_progress(current: int, total: int) -> None:
        pass  # CLI output is per-item; progress bar is for GUI

    print(f"\nProcessing {len(ipsw_files)} IPSW(s) → {args.output}\n")
    results, stats = process_all(ipsw_files, args.output, _on_progress, _on_item)
    print(f"\n{'='*50}")
    print(f"  {stats}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
