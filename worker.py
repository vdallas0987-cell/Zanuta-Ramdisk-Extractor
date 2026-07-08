"""
Background worker for scan and extraction operations.

Runs in a separate QThread to keep the GUI responsive.  Communication
happens exclusively through Qt signals.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from PySide6.QtCore import QThread, Signal

from backend import extract_ramdisk, find_ipsw_files, parse_ipsw
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

    def start_scan(self, directory: Path) -> None:
        """Begin scanning *directory* for IPSW files (async)."""
        self._abort = False
        self._mode = "scan"
        self._directory = Path(directory)
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
        try:
            files = find_ipsw_files(self._directory)
        except NotADirectoryError as exc:
            self.log.emit(str(exc), "ERROR")
            self.scan_finished.emit([], [])
            return

        if not files:
            self.log.emit("Nenhum ficheiro .ipsw válido encontrado na pasta.", "WARNING")
            self.log.emit(
                "Verifique se existem IPSWs dos modelos suportados: "
                "iPhone XS/XR, XS Max, 11, 11 Pro, 11 Pro Max, SE (2ª geração).",
                "INFO",
            )
            self.scan_finished.emit([], [])
            return

        self.log.emit(f"Found {len(files)} IPSW file(s). Parsing manifests…", "INFO")

        valid: List[IPSWInfo] = []
        errors: List[tuple] = []

        for idx, path in enumerate(files):
            if self._abort:
                self.log.emit("Scan cancelled by user.", "WARNING")
                break

            self.progress.emit(idx + 1, len(files))

            try:
                info = parse_ipsw(path)
                if is_a12_a13(info.product_type):
                    valid.append(info)
                    self.scan_item.emit(info)
                    self.log.emit(
                        f"{info.display_name:35s} iOS {info.product_version} ({info.product_build})",
                        "SUCCESS",
                    )
                else:
                    self.log.emit(
                        f"Ignorado: {path.name} — modelo {info.product_type} não é um iPhone A12/A13 suportado.",
                        "WARNING",
                    )
            except Exception as exc:
                self.log.emit(f"Ignorado: {path.name} — {exc}", "ERROR")
                errors.append((path.name, str(exc)))

        self.log.emit(
            f"Scan concluído. {len(valid)} ramdisk(s) A12/A13 disponíveis.",
            "INFO",
        )
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
            def _progress(pct: int) -> None:
                self.file_progress.emit(_path, pct)

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
            elif result.status == ExtractionStatus.ERROR:
                stats.error += 1
                self.log.emit(f"  ERR {result.message}", "ERROR")
            else:
                stats.skipped += 1
                self.log.emit(f"  --  {result.message}", "WARNING")

        self.extraction_finished.emit(stats)
