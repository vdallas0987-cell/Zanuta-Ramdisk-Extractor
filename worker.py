"""
Background worker for scan and extraction operations.

Runs in a separate QThread to keep the GUI responsive.  Communication
happens exclusively through Qt signals.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from PySide6.QtCore import QThread, Signal

from backend import extract_ramdisk, extract_required_components, parse_ipsw, save_unified_metadata
from models import (
    IPSWInfo,
    ExtractionResult,
    ExtractionStatus,
    Stats,
    is_a12_a13,
)


class Worker(QThread):
    """Background worker that runs either a scan or an extraction."""

    # ── Signals ──────────────────────────────────────────────────────

    log = Signal(str, str)            # (message, level)
    progress = Signal(int, int)       # (current, total)
    file_progress = Signal(str, int)  # (ipsw_path, percent 0-100)

    # Scan phase
    scan_item = Signal(object)        # IPSWInfo successfully parsed
    scan_finished = Signal(list, list)  # (valid_IPSWInfos, errors)

    # Extraction phase
    extraction_item = Signal(object)  # ExtractionResult
    extraction_finished = Signal(object)  # Stats

    # ── Public API ───────────────────────────────────────────────────

    def __init__(self, parent=None):
        super().__init__(parent)
        self._abort = False
        self._mode: str = "idle"

    @property
    def mode(self) -> str:
        """Current operation mode: 'idle', 'scan', or 'extract'."""
        return self._mode

    def abort(self) -> None:
        """Request cancellation at the next safe point."""
        self._abort = True

    def start_scan(self, ipsw_path: Path) -> None:
        """Begin loading and parsing a single IPSW file (async)."""
        self._abort = False
        self._mode = "scan"
        self._ipsw_path = Path(ipsw_path)
        self.start()

    def start_extraction(
        self, ipsw_infos: List[IPSWInfo], output_base: Path
    ) -> None:
        """Begin extracting the given ramdisks (async)."""
        self._abort = False
        self._mode = "extract"
        self._ipsw_infos = ipsw_infos
        self._output_base = Path(output_base)
        self.start()

    # ── Run loop (dispatches to phase method) ────────────────────────

    def run(self) -> None:
        if self._mode == "scan":
            self._run_scan()
        elif self._mode == "extract":
            self._run_extraction()

    # ── Scan ─────────────────────────────────────────────────────────

    def _run_scan(self) -> None:
        path = self._ipsw_path

        if not path.is_file():
            self.log.emit(f"File not found: {path}", "ERROR")
            self.scan_finished.emit([], [(path.name, "Not found")])
            return

        self.log.emit(f"Loading {path.name} …", "INFO")

        valid: List[IPSWInfo] = []
        errors: List[tuple] = []

        # Build a progress callback that feeds the Qt signal
        def _scan_progress(pct: int) -> None:
            self.progress.emit(pct, 100)

        if self._abort:
            self.log.emit("Operation cancelled by the user.", "WARNING")
            self.scan_finished.emit(valid, errors)
            return

        self.progress.emit(0, 100)

        try:
            info = parse_ipsw(path, progress_callback=_scan_progress)
        except Exception as exc:
            self.log.emit(f"{path.name} — {exc}", "ERROR")
            errors.append((path.name, str(exc)))
            self.scan_finished.emit(valid, errors)
            return

        if not is_a12_a13(info.product_type):
            self.log.emit(
                f"{path.name} — modelo {info.product_type} is not a supported A12/A13 device.",
                "WARNING",
            )
            self.scan_finished.emit(valid, errors)
            return

        valid.append(info)
        self.scan_item.emit(info)
        self.log.emit(
            f"{info.display_name:35s} iOS {info.product_version} ({info.product_build})",
            "SUCCESS",
        )

        self.log.emit(
            f"{path.name} loaded. Ramdisk ready to extract.",
            "INFO",
        )
        self.progress.emit(100, 100)
        self.scan_finished.emit(valid, errors)

    # ── Extraction ───────────────────────────────────────────────────

    def _run_extraction(self) -> None:
        targets = self._ipsw_infos
        if not targets:
            self.log.emit("Nothing to extract.", "WARNING")
            self.extraction_finished.emit(Stats())
            return

        self.log.emit(
            f"Extracting {len(targets)} ramdisk(s) → {self._output_base} …",
            "INFO",
        )

        stats = Stats(total=len(targets))

        for idx, info in enumerate(targets):
            if self._abort:
                self.log.emit("Extraction cancelled by user.", "WARNING")
                self.extraction_finished.emit(stats)
                return

            self.log.emit(
                f"[{idx + 1}/{len(targets)}] {info.display_name} …", "INFO"
            )
            self.progress.emit(idx + 1, len(targets))

            # Per-file progress callback
            _path = str(info.ipsw_path)
            def _progress(pct: int, _captured_path: str = _path) -> None:
                self.file_progress.emit(_captured_path, pct)

            result = extract_ramdisk(
                info, self._output_base,
                progress_callback=_progress,
            )

            self.extraction_item.emit(result)

            # Ensure progress shows completion
            if result.status == ExtractionStatus.SUCCESS:
                self.file_progress.emit(_path, 100)
            else:
                self.file_progress.emit(_path, -1)  # signal failure
            if result.status == ExtractionStatus.SUCCESS:

                stats.success += 1
                self.log.emit(f"  OK  {result.message}", "SUCCESS")

                # ── Extract required components (iBSS, iBEC, DeviceTree, KernelCache, SEP) ──
                comp_results, comp_stats = extract_required_components(
                    ipsw_path=info.ipsw_path,
                    output_base=self._output_base,
                    product_type=info.product_type,
                    product_version=info.product_version,
                    product_build=info.product_build,
                )
                for cr in comp_results:
                    if cr.status == ExtractionStatus.SUCCESS:
                        self.log.emit(f"    + {cr.message}", "SUCCESS")
                    elif cr.status == ExtractionStatus.ERROR:
                        self.log.emit(f"    ! {cr.message}", "ERROR")

                # ── Unified metadata ──
                try:
                    save_unified_metadata(
                        ipsw_info=info,
                        ramdisk_output=result.output_path,
                        component_results=comp_results,
                        output_base=self._output_base,
                    )
                except Exception as exc:
                    self.log.emit(f"Metadata error: {exc}", "WARNING")

            elif result.status == ExtractionStatus.ERROR:
                stats.error += 1
                self.log.emit(f"  ERR {result.message}", "ERROR")

            else:
                stats.skipped += 1
                self.log.emit(f"  --  {result.message}", "WARNING")
