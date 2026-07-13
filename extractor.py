"""
Extraction logic — stream ramdisks and firmware components from IPSW archives to disk.
"""

from __future__ import annotations

import errno
import hashlib
import os
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from models import (
    ComponentInfo,
    ComponentResult,
    DMGInspection,
    ExtractionResult,
    ExtractionStatus,
    IPSWInfo,
    REQUIRED_COMPONENTS,
    TOOL_VERSION,
    Stats,
    is_a12_a13,
)
from parser import find_all_components, parse_ipsw
from validator import CHUNK_SIZE, _IM4P_COMPONENTS, inspect_dmg, validate_component, validate_dmg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _unique_path(path: Path) -> Path:
    """Return *path* with a ``_N`` suffix if it already exists.

    Uses ``O_CREAT | O_EXCL`` for atomic existence check — eliminates
    the race between ``path.exists()`` and file creation.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    def _try_claim(p: Path) -> bool:
        """Atomically claim *p*. Returns True if we got it."""
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            os.unlink(str(p))   # release so the caller can open it normally
            return True
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            return False

    if _try_claim(path):
        return path

    stem = path.stem
    suffix = path.suffix

    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if _try_claim(candidate):
            return candidate
        counter += 1
def _emit(
    result: ExtractionResult,
    item_callback: Optional[Callable[[ExtractionResult], None]],
    progress_callback: Optional[Callable[[int, int], None]],
    idx: int,
    total: int,
) -> None:
    if item_callback is not None:
        item_callback(result)
    if progress_callback is not None:
        progress_callback(idx + 1, total)


def _compute_sha384(path: Path) -> bytes:
    """Compute SHA-384 digest of a file, returning raw 48-byte digest."""
    h = hashlib.sha384()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


def _verify_digest(path: Path, expected: Optional[bytes]) -> bool:
    """Verify SHA-384 of *path* against *expected*.

    Returns ``True`` if the digests match, ``False`` on mismatch.
    If *expected* is ``None``, returns ``True`` (skip verification).
    """
    if expected is None:
        return True
    actual = _compute_sha384(path)
    return actual == expected


# ---------------------------------------------------------------------------
#  macOS helpers (guarded by sys.platform)
# ---------------------------------------------------------------------------


def _extract_build_manifest(ipsw_path: Path, temp_dir: Path) -> Optional[Path]:
    """Extract ``BuildManifest.plist`` from *ipsw_path* into *temp_dir*.

    Returns the path to the extracted file, or ``None`` on failure.
    """
    try:
        with zipfile.ZipFile(ipsw_path, "r") as zf:
            zf.extract("BuildManifest.plist", temp_dir)
        manifest_path = temp_dir / "BuildManifest.plist"
        if manifest_path.is_file():
            return manifest_path
    except (KeyError, zipfile.BadZipFile, OSError):
        pass
    return None


def _verify_img4_signature(
    build_manifest: Path, component_path: Path,
) -> Optional[bool]:
    """Run ``img4tool --verify`` on a firmware component.

    Returns ``True`` if the signature is valid, ``False`` on mismatch,
    or ``None`` if the tool is unavailable or an error occurred.
    Only meaningful on macOS.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["img4tool", "--verify", str(build_manifest), str(component_path)],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def _check_ramdisk_mountable(dmg_path: Path) -> Optional[bool]:
    """Verify that a DMG is mountable via ``hdiutil attach -nomount``.

    Attaches the image without mounting volumes, checks that a ``/dev/disk``
    device appeared, then detaches it.

    Returns ``True`` if mountable, ``False`` if not, or ``None`` if the
    tool is unavailable.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["hdiutil", "attach", "-nomount", str(dmg_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False
        # Parse all /dev/disk entries and detach each
        devices = []
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and parts[0].startswith("/dev/disk"):
                devices.append(parts[0])
        if not devices:
            return False
        # Detach all claimed devices
        for dev in devices:
            try:
                subprocess.run(
                    ["hdiutil", "detach", dev],
                    capture_output=True, timeout=10,
                )
            except subprocess.SubprocessError:
                pass
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
#  Ramdisk extraction
# ---------------------------------------------------------------------------

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
            total_size = info.file_size or 1

            with zf.open(ipsw_info.ramdisk_path) as src, \
                 open(output_path, "wb") as dst:
                bytes_read = 0
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    bytes_read += len(chunk)
                    if progress_callback is not None:
                        progress_callback(int(bytes_read * 100 / total_size))

                if info.file_size > 0 and bytes_read != info.file_size:
                    logger.warning(
                        "Size mismatch for %s: declared %d bytes, read %d bytes",
                        ipsw_info.ramdisk_path, info.file_size, bytes_read,
                    )

    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        output_path.unlink(missing_ok=True)
        return ExtractionResult(
            ipsw_info=ipsw_info,
            status=ExtractionStatus.ERROR,
            message=f"Extraction failed: {exc}",
        )

    # ── Post-extraction: validation + inspection + metadata + digest ────
    try:
        if not validate_dmg(output_path):
            output_path.unlink(missing_ok=True)
            return ExtractionResult(
                ipsw_info=ipsw_info,
                status=ExtractionStatus.ERROR,
                message="Extracted file is too small or unreadable",
                inspection=None,
            )

        inspection = inspect_dmg(output_path, fast=True)

        # Digest verification
        digest_ok = _verify_digest(output_path, ipsw_info.digest)
        inspection.digest_verified = digest_ok
        if ipsw_info.digest is not None and not digest_ok:
            logger.warning(
                "DIGEST MISMATCH for %s — file may be corrupt or tampered",
                output_path.name,
            )
        elif ipsw_info.digest is not None and digest_ok:
            logger.info("Digest OK: %s", output_path.name)

        logger.info("Inspection: %s — %s", output_path.name, inspection.details)
        save_ramdisk_metadata(ipsw_info, output_path, inspection)

        # macOS: verify the DMG is mountable (debug-level info, never blocks)
        if sys.platform == "darwin":
            mountable = _check_ramdisk_mountable(output_path)
            logger.debug(
                "[DEBUG] Ramdisk mountable: %s",
                mountable if mountable is not None else "N/A (tool not found)",
            )

    except Exception as exc:
        output_path.unlink(missing_ok=True)
        logger.error(
            "Post-extraction step failed for %s: %s",
            output_path.name, exc,
        )
        return ExtractionResult(
            ipsw_info=ipsw_info,
            status=ExtractionStatus.ERROR,
            message=f"Post-extraction failed: {exc}",
            inspection=None,
        )

    return ExtractionResult(
        ipsw_info=ipsw_info,
        status=ExtractionStatus.SUCCESS,
        message=f"Extracted to {output_path}",
        output_path=output_path,
        inspection=inspection,
    )


# ---------------------------------------------------------------------------
#  Metadata export
# ---------------------------------------------------------------------------

def save_ramdisk_metadata(
    ipsw_info: IPSWInfo,
    output_path: Path,
    inspection: Optional[DMGInspection] = None,
) -> Path:
    """Write a JSON metadata file alongside the extracted ramdisk."""
    meta: dict = {
        "device": {
            "product_type": ipsw_info.product_type,
            "device_name": ipsw_info.device_name,
            "display_name": ipsw_info.display_name,
        },
        "firmware": {
            "ios_version": ipsw_info.product_version,
            "build": ipsw_info.product_build,
            "source_ipsw": ipsw_info.ipsw_path.name,
            "source_ipsw_path": str(ipsw_info.ipsw_path.resolve()),
        },
        "ramdisk": {
            "original_path_in_zip": ipsw_info.ramdisk_path,
            "output_filename": output_path.name,
            "output_relative_path": str(ipsw_info.output_relative_path),
            "output_absolute_path": str(output_path.resolve()),
        },
        "extraction": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "Zanuta Ramdisk Extractor",
            "tool_version": TOOL_VERSION,
        },
    }

    if inspection is not None:
        meta["inspection"] = {
            "format": inspection.format_name,
            "file_size_bytes": inspection.file_size,
            "file_size_mb": round(inspection.file_size / (1024 * 1024), 1),
            "structure_valid": inspection.structure_valid,
            "filesystem": inspection.filesystem,
            "container_size_bytes": inspection.container_size,
            "encrypted": inspection.encrypted,
            "digest_verified": inspection.digest_verified,
            "details": inspection.details,
        }

    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.debug("Metadata saved: %s", json_path.name)
    return json_path


# ---------------------------------------------------------------------------
#  All-components extraction
# ---------------------------------------------------------------------------

def extract_all_components(
    ipsw_path: Path,
    output_base: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    item_callback: Optional[Callable[[ComponentResult], None]] = None,
) -> tuple[list[ComponentResult], Stats]:
    """Extract every firmware component referenced in the BuildManifest.

    Output structure::

        <output_base>/
          <ProductType>/
            <Version>/
              components/
                <ComponentName>/
                  <filename>
                  metadata.json
    """
    ipsw_path = Path(ipsw_path)
    output_base = Path(output_base)

    # ── Parse ──────────────────────────────────────────────────────
    try:
        ipsw_info_from_scan = parse_ipsw(ipsw_path)
    except Exception as exc:
        logger.error("Failed to parse %s: %s", ipsw_path.name, exc)
        return [], Stats(total=1, error=1)

    product_type = ipsw_info_from_scan.product_type
    product_version = ipsw_info_from_scan.product_version
    product_build = ipsw_info_from_scan.product_build

    # ── Discover components ────────────────────────────────────────
    components = find_all_components(ipsw_path)
    if not components:
        logger.warning("No components found in %s", ipsw_path.name)
        return [], Stats(total=0)

    stats = Stats(total=len(components))
    results: list[ComponentResult] = []

    components_dir = output_base / product_type / product_version / "components"

    # ── macOS: extract BuildManifest.plist for img4tool verification ──
    _build_manifest: Optional[Path] = None
    _tmp_dir: Optional[Path] = None
    if sys.platform == "darwin":
        _tmp_dir = Path(tempfile.mkdtemp(prefix="buildmaniest_"))
        _build_manifest = _extract_build_manifest(ipsw_path, _tmp_dir)
        if _build_manifest is None:
            logger.debug("BuildManifest.plist not found — IMG4 verification skipped")

    for idx, (key, zip_path, info_dict, digest) in enumerate(components):
        if progress_callback is not None:
            progress_callback(idx + 1, len(components))

        is_fw = info_dict.get("IsFirmwarePayload", False)
        is_sec = info_dict.get("IsSecondaryFirmwarePayload", False)

        comp_info = ComponentInfo(
            name=key,
            path_in_zip=zip_path,
            product_type=product_type,
            product_version=product_version,
            product_build=product_build,
            source_ipsw=ipsw_path,
            is_firmware_payload=bool(is_fw),
            is_secondary_payload=bool(is_sec),
            digest=digest,
        )

        filename = Path(zip_path).name
        output_path = _unique_path(components_dir / key / filename)

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            result = ComponentResult(
                component=comp_info,
                status=ExtractionStatus.ERROR,
                message=f"Cannot create directory: {exc}",
            )
            results.append(result)
            stats.error += 1
            if item_callback is not None:
                item_callback(result)
            continue

        # ── Stream extraction ──────────────────────────────────────
        try:
            with zipfile.ZipFile(ipsw_path, "r") as zf:
                zinfo = zf.getinfo(zip_path)
                total_size = zinfo.file_size or 1
                with zf.open(zip_path) as src, open(output_path, "wb") as dst:
                    bytes_read = 0
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        bytes_read += len(chunk)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            result = ComponentResult(
                component=comp_info,
                status=ExtractionStatus.ERROR,
                message=f"Extraction failed: {exc}",
            )
            results.append(result)
            stats.error += 1
            if item_callback is not None:
                item_callback(result)
            continue

        # ── Digest verification ────────────────────────────────────
        digest_ok = _verify_digest(output_path, digest)
        if digest is not None and not digest_ok:
            logger.warning(
                "DIGEST MISMATCH for component '%s' — %s", key, output_path.name,
            )
        elif digest is not None and digest_ok:
            logger.debug("Digest OK: component '%s' — %s", key, output_path.name)

        # IMG4 signature verification (macOS only, IM4P components)
        _signature_verified: Optional[bool] = None
        if _build_manifest is not None and key in _IM4P_COMPONENTS:
            _signature_verified = _verify_img4_signature(_build_manifest, output_path)
            if _signature_verified is True:
                logger.debug("IMG4 signature OK: %s — %s", key, output_path.name)
            elif _signature_verified is False:
                logger.warning("IMG4 signature INVALID: %s — %s", key, output_path.name)

        # ── Metadata ───────────────────────────────────────────────
        try:
            _save_component_metadata(
                comp_info, output_path,
                digest_verified=digest_ok,
                signature_verified=_signature_verified,
            )
        except Exception as exc:
            logger.warning("Failed to save metadata for %s: %s", key, exc)

        result = ComponentResult(
            component=comp_info,
            status=ExtractionStatus.SUCCESS,
            message=f"Extracted to {output_path}",
            output_path=output_path,
            digest_verified=digest_ok,
        )
        results.append(result)
        stats.success += 1

        if item_callback is not None:
            item_callback(result)

    # Clean up temporary BuildManifest.plist
    if _tmp_dir is not None:
        try:
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        except OSError:
            pass

    return results, stats


def _save_component_metadata(
    comp_info: ComponentInfo,
    output_path: Path,
    digest_verified: Optional[bool] = None,
    signature_verified: Optional[bool] = None,
) -> Path:
    """Write a JSON metadata file alongside an extracted component."""
    meta = {
        "component": {
            "name": comp_info.name,
            "path_in_zip": comp_info.path_in_zip,
            "is_firmware_payload": comp_info.is_firmware_payload,
            "is_secondary_payload": comp_info.is_secondary_payload,
        },
        "device": {
            "product_type": comp_info.product_type,
        },
        "firmware": {
            "ios_version": comp_info.product_version,
            "build": comp_info.product_build,
            "source_ipsw": comp_info.source_ipsw.name,
        },
        "extraction": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "Zanuta Ramdisk Extractor",
            "tool_version": TOOL_VERSION,
        },
    }

    if comp_info.digest is not None:
        meta["digest"] = {
            "expected_sha384": comp_info.digest.hex(),
            "verified": digest_verified,
        }

    if signature_verified is not None:
        meta["signature_verified"] = signature_verified

    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return json_path


# ---------------------------------------------------------------------------
#  Required-components extraction (iBSS, iBEC, DeviceTree, KernelCache, SEP)
# ---------------------------------------------------------------------------

def extract_required_components(
    ipsw_path: Path,
    output_base: Path,
    product_type: str,
    product_version: str,
    product_build: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    item_callback: Optional[Callable[[ComponentResult], None]] = None,
) -> tuple[list[ComponentResult], Stats]:
    """Extract the 5 required firmware components from an IPSW.

    Components are saved alongside the ramdisk output::

        <output_base>/
          <ProductType>/
            <Version>/
              <ProductType>_<Version>_<Build>_iBSS.img4
              <ProductType>_<Version>_<Build>_iBEC.img4
              ...
    """
    ipsw_path = Path(ipsw_path)
    output_base = Path(output_base)

    # Discover all components from the BuildManifest
    all_components = find_all_components(ipsw_path)
    if not all_components:
        return [], Stats(total=0)

    # Build a lookup: manifest_key -> (zip_path, info_dict, digest)
    comp_lookup: dict[str, tuple[str, dict, Optional[bytes]]] = {
        key: (zip_path, info, digest)
        for key, zip_path, info, digest in all_components
    }

    # Filter required components, deduplicating by output filename
    # (RestoreSEP is preferred over SEP when both exist)
    target_keys: list[str] = []
    seen_outputs: set[str] = set()
    for k in REQUIRED_COMPONENTS:
        if k not in comp_lookup:
            continue
        output_name = REQUIRED_COMPONENTS[k]
        if output_name in seen_outputs:
            continue
        seen_outputs.add(output_name)
        target_keys.append(k)
    if not target_keys:
        logger.info("None of the required components found in %s", ipsw_path.name)
        return [], Stats(total=0)

    stats = Stats(total=len(target_keys))
    results: list[ComponentResult] = []

    base_path = output_base / product_type / product_version

    # ── macOS: extract BuildManifest.plist for img4tool verification ──
    _build_manifest: Optional[Path] = None
    _tmp_dir: Optional[Path] = None
    if sys.platform == "darwin":
        _tmp_dir = Path(tempfile.mkdtemp(prefix="buildmaniest_"))
        _build_manifest = _extract_build_manifest(ipsw_path, _tmp_dir)
        if _build_manifest is None:
            logger.debug("BuildManifest.plist not found in %s — IMG4 verification skipped", ipsw_path.name)

    for idx, key in enumerate(target_keys):
        if progress_callback is not None:
            progress_callback(idx + 1, len(target_keys))

        zip_path, info, digest = comp_lookup[key]
        output_name = REQUIRED_COMPONENTS[key]
        output_path = _unique_path(base_path / output_name)

        # Build ComponentInfo
        is_fw = info.get("IsFirmwarePayload", False)
        is_sec = info.get("IsSecondaryFirmwarePayload", False)
        comp_info = ComponentInfo(
            name=key,
            path_in_zip=zip_path,
            product_type=product_type,
            product_version=product_version,
            product_build=product_build,
            source_ipsw=ipsw_path,
            is_firmware_payload=bool(is_fw),
            is_secondary_payload=bool(is_sec),
            digest=digest,
        )

        # Ensure output directory exists
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            result = ComponentResult(
                component=comp_info,
                status=ExtractionStatus.ERROR,
                message=f"Cannot create directory: {exc}",
            )
            results.append(result)
            stats.error += 1
            if item_callback is not None:
                item_callback(result)
            continue

        # Stream extraction
        try:
            with zipfile.ZipFile(ipsw_path, "r") as zf:
                zinfo = zf.getinfo(zip_path)
                total_size = zinfo.file_size or 1
                with zf.open(zip_path) as src, open(output_path, "wb") as dst:
                    bytes_read = 0
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        bytes_read += len(chunk)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            result = ComponentResult(
                component=comp_info,
                status=ExtractionStatus.ERROR,
                message=f"Extraction failed: {exc}",
            )
            results.append(result)
            stats.error += 1
            if item_callback is not None:
                item_callback(result)
            continue

        # Digest verification
        digest_ok = _verify_digest(output_path, digest)
        if digest is not None and not digest_ok:
            logger.warning(
                "DIGEST MISMATCH for '%s' — %s", key, output_path.name,
            )

        # Magic-byte validation
        validation_warning = validate_component(output_path, key)
        if validation_warning:
            logger.warning("VALIDATION: %s", validation_warning)

        # IMG4 signature verification (macOS only, IM4P components)
        _signature_verified: Optional[bool] = None
        if _build_manifest is not None and key in _IM4P_COMPONENTS:
            _signature_verified = _verify_img4_signature(_build_manifest, output_path)
            logger.info("img4tool signature check for %s: %s", key, _signature_verified)

        # Per-component metadata (sidecar .json)
        try:
            _save_component_metadata(
                comp_info, output_path,
                digest_verified=digest_ok,
                signature_verified=_signature_verified,
            )
        except Exception as exc:
            logger.warning("Failed to save metadata for %s: %s", key, exc)
        result = ComponentResult(
            component=comp_info,
            status=ExtractionStatus.SUCCESS,
            message=f"Extracted to {output_path}",
            output_path=output_path,
        )
        results.append(result)
        stats.success += 1
        if item_callback is not None:
            item_callback(result)

    # Clean up temporary BuildManifest.plist
    if _tmp_dir is not None:
        try:
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        except OSError:
            pass

    return results, stats


def save_unified_metadata(
    ipsw_info: IPSWInfo,
    ramdisk_output: Optional[Path],
    component_results: list[ComponentResult],
    output_base: Path,
) -> Optional[Path]:
    """Write a single ``metadata.json`` at the version directory level.

    The file aggregates info about the device, firmware, extracted ramdisk,
    and every extracted firmware component into one place.
    """
    base_path = output_base / ipsw_info.product_type / ipsw_info.product_version
    json_path = base_path / "metadata.json"

    components_list = []
    for cr in component_results:
        c = cr.component
        entry: dict = {
            "name": c.name,
            "output_filename": cr.output_path.name if cr.output_path else None,
            "path_in_zip": c.path_in_zip,
            "status": cr.status.name,
        }
        if c.digest is not None:
            entry["digest_verified"] = cr.digest_verified
        components_list.append(entry)

    meta: dict = {
        "device": {
            "product_type": ipsw_info.product_type,
            "device_name": ipsw_info.device_name,
            "display_name": ipsw_info.display_name,
        },
        "firmware": {
            "ios_version": ipsw_info.product_version,
            "build": ipsw_info.product_build,
            "source_ipsw": ipsw_info.ipsw_path.name,
            "source_ipsw_path": str(ipsw_info.ipsw_path.resolve()),
        },
        "ramdisk": {
            "output_filename": ramdisk_output.name if ramdisk_output else None,
            "output_relative_path": str(ipsw_info.output_relative_path),
        },
        "components": components_list,
        "extraction": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "Zanuta Ramdisk Extractor",
            "tool_version": TOOL_VERSION,
        },
    }

    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Unified metadata saved: %s", json_path)
        return json_path
    except (OSError, PermissionError) as exc:
        logger.warning("Failed to write unified metadata: %s", exc)
        return None

# ---------------------------------------------------------------------------
#  Batch orchestrator
# ---------------------------------------------------------------------------

def process_all(
    ipsw_paths: List[Path],
    output_base: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    item_callback: Optional[Callable[[ExtractionResult], None]] = None,
) -> Tuple[List[ExtractionResult], Stats]:
    """Process a batch of IPSW files sequentially."""
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
                inspection=None,
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
                inspection=None,
            )
            stats.skipped += 1
            results.append(result)
            _emit(result, item_callback, progress_callback, idx, len(ipsw_paths))
            continue


        # --- Extract ramdisk ---
        result = extract_ramdisk(ipsw_info, output_base)
        if result.status == ExtractionStatus.SUCCESS:
            stats.success += 1
        else:
            stats.error += 1

        # --- Extract required components (iBSS, iBEC, DeviceTree, KernelCache, SEP) ---
        comp_results, comp_stats = extract_required_components(
            ipsw_path=ipsw_info.ipsw_path,
            output_base=output_base,
            product_type=ipsw_info.product_type,
            product_version=ipsw_info.product_version,
            product_build=ipsw_info.product_build,
        )
        if comp_stats.total > 0:
            logger.info(
                "Components: %d ok, %d errors",
                comp_stats.success, comp_stats.error,
            )
            for cr in comp_results:
                if cr.status == ExtractionStatus.SUCCESS:
                    logger.info("  + %s", cr.message)
                elif cr.status == ExtractionStatus.ERROR:
                    logger.warning("  ! %s", cr.message)

        # --- Save unified metadata ---
        try:
            # Build a temporary IPSWInfo-like object for metadata purposes
            # using the already-parsed info
            save_unified_metadata(
                ipsw_info=ipsw_info,
                ramdisk_output=result.output_path,
                component_results=comp_results,
                output_base=output_base,
            )
        except Exception as exc:
            logger.warning("Failed to save unified metadata: %s", exc)

        results.append(result)
        _emit(result, item_callback, progress_callback, idx, len(ipsw_paths))

    return results, stats
