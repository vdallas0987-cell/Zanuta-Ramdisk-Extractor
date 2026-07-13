"""
Backward-compatible re-export facade.

All public symbols from ``scanner``, ``parser``, ``extractor``, and
``validator`` are re-exported here so that existing code (``worker.py``,
``app.py``, test files) continues to work without changes.
"""

from __future__ import annotations

# Re-export everything from the split modules
from extractor import (
    _emit,
    _compute_sha384,
    _check_ramdisk_mountable,
    _extract_build_manifest,
    _save_component_metadata,
    _unique_path,
    _verify_img4_signature,
    _verify_digest,
    extract_all_components,
    extract_ramdisk,
    extract_required_components,
    save_unified_metadata,
    process_all,
    save_ramdisk_metadata,
)
from parser import (
    _ProgressFileWrapper,
    _extract_path,
    _extract_plist,
    _find_best_identity,
    _find_ramdisk_in_manifest,
    _get_device_info,
    _search_manifest_for_ramdisk,
    _verify_ramdisk_in_zip,
    find_all_components as _find_all_components,
    find_all_components,
    parse_ipsw,
)
from scanner import _is_zip_file, find_ipsw_files, find_ipsws
from validator import (
    CHUNK_SIZE,
    _IM4P_COMPONENTS,
    _check_signatures,
    _inspect_apfs,
    _inspect_gpt,
    _inspect_hfs,
    _inspect_udif,
    _read_uint32,
    _read_uint64,
    _try_hdiutil,
    inspect_dmg,
    validate_dmg,
)

# ---------------------------------------------------------------------------
#  CLI entry point (for testing without GUI)
# ---------------------------------------------------------------------------

import argparse
import logging as _logging
import sys
from pathlib import Path as _Path

from models import ExtractionResult, ExtractionStatus, is_a12_a13


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract restore ramdisks from iPhone A12/A13 IPSWs.",
    )
    parser.add_argument(
        "input_dir",
        type=_Path,
        help="Directory containing .ipsw files (scanned recursively)",
    )
    parser.add_argument(
        "-o", "--output",
        type=_Path,
        default=_Path("./ramdisks"),
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
    level = _logging.WARNING
    if args.verbose >= 2:
        level = _logging.DEBUG
    elif args.verbose >= 1:
        level = _logging.INFO

    _logging.basicConfig(
        level=level,
        format="%(levelname).1s %(message)s",
    )

    # --- Scan ---
    try:
        ipsw_files = find_ipsw_files(args.input_dir)
    except NotADirectoryError as exc:
        _logging.exception(exc)
        raise SystemExit(1) from exc

    if not ipsw_files:
        _logging.warning("No .ipsw files found in %s", args.input_dir)
        raise SystemExit(0)

    # --- Dry-run or process ---
    if args.dry_run:
        print(f"Dry-run: {len(ipsw_files)} IPSW(s) found\n")
        for ipsw_path in ipsw_files:
            try:
                info = parse_ipsw(ipsw_path)
                verdict = "[OK] A12/A13" if is_a12_a13(info.product_type) else "[ERR] skipped"
                print(f"  [{verdict}] {info.display_name:35s}  iOS {info.product_version} ({info.product_build})")
            except Exception as exc:
                print(f"  [ERROR] {ipsw_path.name}: {exc}")
        return

    # --- Process ---
    def _on_item(result: ExtractionResult) -> None:
        icon = {
            ExtractionStatus.SUCCESS: "[OK]",
            ExtractionStatus.ERROR:   "[ERR]",
            ExtractionStatus.SKIPPED: "[-]",
        }.get(result.status, "?")
        print(f"  [{icon}] {result.ipsw_info.display_name:35s}  {result.message}")

    def _on_progress(current: int, total: int) -> None:
        pass

    print(f"\nProcessing {len(ipsw_files)} IPSW(s) -> {args.output}\n")
    results, stats = process_all(ipsw_files, args.output, _on_progress, _on_item)
    print(f"\n{'='*50}")
    print(f"  {stats}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
