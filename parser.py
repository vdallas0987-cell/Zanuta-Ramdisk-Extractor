"""
BuildManifest.plist parsing — extract device metadata, digests, and component paths
from IPSW archives.
"""

from __future__ import annotations

import io
import logging
import os
import plistlib
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

from models import IPSWInfo, is_a12_a13

logger = logging.getLogger(__name__)

# ── Manifest cache (avoids re-reading the ZIP central directory) ─────────
# Key: (real_path, mtime)  →  Value: (manifest_dict, namelist)
_MANIFEST_CACHE: dict[tuple[str, float], tuple[dict, list[str]]] = {}



# ---------------------------------------------------------------------------
#  Progressive ZIP reader  (reports progress during central-directory scan)
# ---------------------------------------------------------------------------

class _ProgressFileWrapper(io.RawIOBase):
    """Wraps a binary file and calls ``progress_callback(percent)`` as data is read.

    Designed to be passed to ``zipfile.ZipFile`` so that the central-directory
    scan (which happens at open time for large IPSWs) reports real progress.
    """

    def __init__(
        self,
        path: Path,
        progress_callback: Callable[[int], None] | None = None,
        file_size: int | None = None,
    ) -> None:
        super().__init__()
        self._fp = open(path, "rb")
        self._size = file_size or os.fstat(self._fp.fileno()).st_size
        self._callback = progress_callback
        self._last_reported = -1
        self._total_read = 0

    # ── RawIOBase interface ────────────────────────────────────────

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        n = self._fp.readinto(b)
        if n is not None:
            self._total_read += n
            self._maybe_report()
        return n if n is not None else 0

    def read(self, n: int = -1) -> bytes:
        data = self._fp.read(n)
        self._total_read += len(data)
        self._maybe_report()
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        result = self._fp.seek(offset, whence)
        mod_total = self._total_read
        if whence == os.SEEK_SET:
            mod_total = result
        elif whence == os.SEEK_END:
            mod_total = self._size + result
        self._total_read = mod_total
        return result

    def tell(self) -> int:
        return self._fp.tell()

    def close(self) -> None:
        try:
            self._fp.close()
        finally:
            super().close()

    # ── Internal ───────────────────────────────────────────────────

    def _maybe_report(self) -> None:
        if self._callback is None:
            return
        pct = min(int(self._total_read * 100 / self._size), 100)
        if pct != self._last_reported:
            self._last_reported = pct
            self._callback(pct)


# ---------------------------------------------------------------------------
#  BuildManifest parsing
# ---------------------------------------------------------------------------

def _extract_plist(zf: zipfile.ZipFile) -> dict:
    """Read ``BuildManifest.plist`` from *zf*, trying common path variations."""
    candidates = ["BuildManifest.plist"]
    candidates.extend(
        n for n in zf.namelist() if n.endswith("/BuildManifest.plist")
    )

    for candidate in dict.fromkeys(candidates):
        try:
            with zf.open(candidate) as fh:
                data: dict = plistlib.load(fh)
            logger.debug("Read BuildManifest.plist from '%s'", candidate)
            return data
        except (KeyError, plistlib.InvalidFileException):
            continue

    raise ValueError("BuildManifest.plist not found inside IPSW")


def _extract_path(entry: dict) -> Optional[str]:
    """Extract ``Path`` from a Manifest entry."""
    inner = entry.get("Info") or entry.get("info") or {}
    return inner.get("Path") or inner.get("path") or None


def _get_digest(entry: dict) -> Optional[bytes]:
    """Extract the ``Digest`` (SHA-384, 48 bytes) from a Manifest entry.

    Returns ``None`` if no digest is present.
    """
    return entry.get("Digest") or None


def _find_best_identity(manifest: dict) -> Optional[dict]:
    """Return the best BuildIdentity (preferring Erase over Update)."""
    identities = manifest.get("BuildIdentities", [])
    if not identities:
        return None
    scored: list[tuple[int, dict]] = []
    for identity in identities:
        behavior = identity.get("Info", {}).get("RestoreBehavior", "")
        priority = {"Erase": 0, "Update": 1}.get(behavior, 2)
        scored.append((priority, identity))
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def _get_device_info(plist: dict) -> Optional[tuple[dict, dict]]:
    """Return (device_info, identity) from the best BuildIdentity."""
    valid_identities: list[tuple[dict, dict, int]] = []

    for identity in plist.get("BuildIdentities", []):
        product_type = (
            identity.get("Ap,ProductType")
            or identity.get("Info", {}).get("ProductType")
        )
        if not product_type:
            continue

        ramdisk_path = _find_ramdisk_in_manifest(identity)
        if ramdisk_path is None:
            continue

        behavior = identity.get("Info", {}).get("RestoreBehavior", "")
        priority = {"Erase": 0, "Update": 1}.get(behavior, 2)

        valid_identities.append(({
            "product_type": product_type,
            "version": plist.get("ProductVersion", "unknown"),
            "build": plist.get("ProductBuildVersion", "unknown"),
            "ramdisk_path": ramdisk_path,
        }, identity, priority))

    if not valid_identities:
        return None, None

    valid_identities.sort(key=lambda x: x[2])
    return valid_identities[0][0], valid_identities[0][1]


# ---------------------------------------------------------------------------
#  Ramdisk discovery
# ---------------------------------------------------------------------------

def _find_ramdisk_in_manifest(identity: dict) -> Optional[str]:
    """Find the ramdisk path inside a BuildIdentity."""
    manifest = identity.get("Manifest") or identity.get("manifest") or {}

    # Direct
    ramdisk_info = manifest.get("RestoreRamDisk") or manifest.get("RestoreRamdisk")
    if ramdisk_info is not None:
        path = _extract_path(ramdisk_info)
        if path:
            return path

    # Via "Restore" wrapper
    restore = manifest.get("Restore") or manifest.get("restore") or {}
    for key in (
        "RestoreRamDisk", "RestoreRamdisk",
        "Restore RamDisk", "Restore ramdisk",
    ):
        ramdisk_info = restore.get(key)
        if ramdisk_info is None:
            continue
        path = _extract_path(ramdisk_info)
        if path:
            return path

    # Fallback recursive scan
    logger.debug("Known keys failed — starting recursive Manifest scan")
    return _search_manifest_for_ramdisk(manifest)


def _search_manifest_for_ramdisk(manifest: dict) -> Optional[str]:
    """Recursively scan the Manifest for a ramdisk entry."""
    if not isinstance(manifest, dict):
        return None

    candidates: list[tuple[str, str]] = []

    def _recurse(d: dict, depth: int = 0) -> None:
        if depth > 5:
            return
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            path = _extract_path(v)
            if path:
                candidates.append((k, path))
            _recurse(v, depth + 1)

    _recurse(manifest)

    if not candidates:
        return None

    def _score(item: tuple[str, str]) -> int:
        key, path = item
        score = 0
        kl = key.lower()
        if "ram" in kl:
            score += 2
        if "disk" in kl or "dmg" in kl:
            score += 2
        if path.lower().endswith(".dmg"):
            score += 1
        return score

    candidates.sort(key=_score, reverse=True)
    best = candidates[0]
    return best[1] if _score(best) >= 1 else None


def _verify_ramdisk_in_zip(zf: zipfile.ZipFile, ramdisk_path: str) -> str:
    """Return the canonical path for *ramdisk_path* inside *zf*."""
    if ramdisk_path in zf.namelist():
        return ramdisk_path

    all_names = zf.namelist()
    matches = [n for n in all_names if n.endswith(ramdisk_path)]

    if not matches:
        raise KeyError(f"Ramdisk path '{ramdisk_path}' not found inside IPSW")

    if len(matches) > 1:
        logger.warning(
            "Ambiguous ramdisk path '%s' — multiple matches: %s. "
            "Using the first match.",
            ramdisk_path, matches,
        )

    logger.debug("Resolved ramdisk path: '%s' -> '%s'", ramdisk_path, matches[0])
    return matches[0]


# ---------------------------------------------------------------------------
#  Helper: find the Manifest key whose Info.Path matches a target
# ---------------------------------------------------------------------------

def _find_manifest_key_for_path(manifest: dict, target_path: str) -> Optional[str]:
    """Search *manifest* for an entry whose ``Info.Path`` equals *target_path*.

    Returns the manifest key (e.g. ``"RestoreRamDisk"``) or ``None``.
    """
    for key, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        path = _extract_path(entry)
        if path == target_path:
            return key
    return None


# ---------------------------------------------------------------------------
#  Top-level parsing  (with optional progress reporting)
# ---------------------------------------------------------------------------


def _cached_parse(ipsw_path: Path) -> tuple[dict, list[str]]:
    """Return (manifest, namelist) for *ipsw_path*, using a cache to avoid
    re-reading the ZIP central directory on repeated calls.

    The cache key is ``(resolved_path, mtime)``, so it invalidates
    automatically when the file changes.
    """
    ipsw_path = ipsw_path.resolve()
    mtime = ipsw_path.stat().st_mtime
    key = (str(ipsw_path), mtime)

    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        return cached

    with zipfile.ZipFile(ipsw_path, "r") as zf:
        manifest = _extract_plist(zf)
        namelist = zf.namelist()

    _MANIFEST_CACHE[key] = (manifest, namelist)
    return manifest, namelist
def parse_ipsw(
    ipsw_path: Path,
    progress_callback: Callable[[int], None] | None = None,
) -> IPSWInfo:
    """Open *ipsw_path* and return parsed metadata.

    When *progress_callback* is provided, it receives an ``int 0-100``
    reflecting the proportion of the ZIP central directory read so far.
    This is especially useful for large IPSWs (2-6 GB).

    Raises
    ------
    FileNotFoundError, ValueError, zipfile.BadZipFile
    """
    ipsw_path = Path(ipsw_path)
    if not ipsw_path.is_file():
        raise FileNotFoundError(f"IPSW not found: {ipsw_path}")

    try:
        # Use progressive wrapper when a progress callback is provided
        wrapper: _ProgressFileWrapper | None = None
        if progress_callback is not None:
            wrapper = _ProgressFileWrapper(ipsw_path, progress_callback)
            zf = zipfile.ZipFile(wrapper, "r")
        else:
            zf = zipfile.ZipFile(ipsw_path, "r")

        with zf:
            manifest = _extract_plist(zf)

            device_info, identity = _get_device_info(manifest)
            if device_info is None:
                raise ValueError(
                    "No BuildIdentity contains valid ProductType and RestoreRamDisk"
                )

            product_type = device_info["product_type"]
            product_version = device_info["version"]
            product_build = device_info["build"]
            ramdisk_declared = device_info["ramdisk_path"]
            ramdisk_path = _verify_ramdisk_in_zip(zf, ramdisk_declared)

            # Signal completion
            if progress_callback is not None:
                progress_callback(100)

            logger.debug(
                "Parsed %s — %s / %s (%s)  ramdisk=%s",
                ipsw_path.name, product_type, product_version, product_build,
                ramdisk_path,
            )

            # ── Extract digest ────────────────────────────────────────
            # Use the identity that _get_device_info() actually selected,
            # and find the correct Manifest key (not the raw path value).
            raw_manifest = identity.get("Manifest") or {}
            ramdisk_key = _find_manifest_key_for_path(raw_manifest, ramdisk_declared)
            if ramdisk_key is not None:
                ramdisk_entry = raw_manifest.get(ramdisk_key, {})
            else:
                logger.warning(
                    "Could not find Manifest key for ramdisk path '%s'",
                    ramdisk_declared,
                )
                ramdisk_entry = {}
            digest = _get_digest(ramdisk_entry)

            # Populate manifest cache so find_all_components() can reuse it
            _MANIFEST_CACHE[(str(ipsw_path.resolve()), ipsw_path.stat().st_mtime)] = (manifest, zf.namelist())

            return IPSWInfo(
                ipsw_path=ipsw_path,
                product_type=product_type,
                product_version=product_version,
                product_build=product_build,
                ramdisk_path=ramdisk_path,
                digest=digest,
            )

    except zipfile.BadZipFile as exc:
        raise ValueError(f"Corrupt ZIP (not a valid IPSW): {exc}") from exc



def find_all_components(
    ipsw_path: Path,
    progress_callback: Callable[[int], None] | None = None,
) -> list[tuple[str, str, dict, Optional[bytes]]]:
    """Scan BuildManifest and return all component entries with a valid Path.

    Uses the same BuildIdentity selection as :func:`parse_ipsw` (via
    ``_get_device_info``) to guarantee consistency between ramdisk and
    component extraction.

    Returns
    -------
    list of (manifest_key, zip_path, info_dict, digest_bytes)
    """
    ipsw_path = Path(ipsw_path)

    manifest, all_names = _cached_parse(ipsw_path)
    _, identity = _get_device_info(manifest)
    if identity is None:
        return []

    raw_manifest = identity.get("Manifest") or identity.get("manifest") or {}
    components: list[tuple[str, str, dict, Optional[bytes]]] = []

    for key, entry in raw_manifest.items():
        if not isinstance(entry, dict):
            continue
        path = _extract_path(entry)
        if not path:
            continue

        if path not in all_names:
            matches = [n for n in all_names if n.endswith(path)]
            if not matches:
                logger.warning(
                    "Component '%s': declared path '%s' not found in ZIP",
                    key, path,
                )
                continue
            resolved = matches[0]
            if len(matches) > 1:
                logger.debug(
                    "Component '%s': ambiguous path '%s', using '%s'",
                    key, path, resolved,
                )
        else:
            resolved = path

        info = entry.get("Info") or entry.get("info") or {}
        digest = _get_digest(entry)
        components.append((key, resolved, info, digest))

    if progress_callback is not None:
        progress_callback(100)

    return components
