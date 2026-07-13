"""
IPSW file discovery — find and validate ``.ipsw`` archives on disk.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def _is_zip_file(path: Path) -> bool:
    """Check whether *path* is a valid ZIP archive.

    Uses ``zipfile.is_zipfile()`` which handles:
      * Regular archives starting with ``PK\x03\x04``
      * Empty ZIP archives starting with ``PK\x05\x06`` (EOCD-only)
      * Multi-volume ZIP archives
    """
    try:
        return zipfile.is_zipfile(path)
    except (OSError, ValueError):
        return False


def find_ipsws(folder: str, recursive: bool = True) -> List[Path]:
    """Discover valid ``.ipsw`` files under *folder*.

    Performs a **case-insensitive** search (accepts ``.IPSW``, ``.Ipsw``, etc.)
    and validates that each candidate is actually a ZIP file.

    Raises
    ------
    NotADirectoryError
        If *directory* does not exist or is not a folder.
    """
    folder = Path(folder).resolve(strict=False)
    if not folder.is_dir():
        raise NotADirectoryError(f"Directory not found: {folder}")

    pattern = "**/*" if recursive else "*"
    candidates = [
        p for p in folder.glob(pattern)
        if p.is_file() and p.suffix.lower() == ".ipsw"
    ]

    valid: List[Path] = []
    for p in candidates:
        if _is_zip_file(p):
            valid.append(p)
        else:
            logger.warning(
                "Skipped: %s — invalid .ipsw file (not a valid ZIP)", p.name,
            )

    logger.info("Found %d valid IPSW file(s) in %s", len(valid), folder)
    return sorted(valid)


# Keep old name as an alias for backward compatibility
find_ipsw_files = find_ipsws
