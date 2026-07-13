"""
Graphical interface for Zanuta Ramdisk Extractor.

Provides a PySide6 window with a table of found IPSWs, a coloured log
panel, and background threading so the UI stays responsive during
scanning and extraction.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import (
    QSize,
    QSettings,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QMainWindow,
    QMenuBar,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from models import (
    TOOL_VERSION,
    IPSWInfo,
    ExtractionResult,
    ExtractionStatus,
    Stats,
    A12_A13_DEVICES,
    DEVICE_NAMES,
)
from worker import Worker

# ──────────────────────────────────────────────────────────────────────
#  Colour palette for log levels
# ──────────────────────────────────────────────────────────────────────

_LOG_COLORS = {
    "SUCCESS": QColor("#1a7f37"),
    "INFO":    QColor("#1f2328"),
    "WARNING": QColor("#9a6700"),
    "ERROR":   QColor("#cf222e"),
}

_STATUS_COLORS = {
    ExtractionStatus.SUCCESS:   QColor("#1a7f37"),
    ExtractionStatus.ERROR:     QColor("#cf222e"),
    ExtractionStatus.SKIPPED:   QColor("#9a6700"),
    ExtractionStatus.EXTRACTING: QColor("#0969da"),
    ExtractionStatus.READY:     QColor("#1f2328"),
}

_STATUS_LABELS = {
    ExtractionStatus.SUCCESS:    "Done",
    ExtractionStatus.ERROR:      "Error",
    ExtractionStatus.SKIPPED:    "Skipped",
    ExtractionStatus.EXTRACTING: "Extracting …",
    ExtractionStatus.READY:      "Ready",
}


# ──────────────────────────────────────────────────────────────────────
#  Main window
# ──────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level window for the Ramdisk Extractor."""

    def __init__(self) -> None:
        super().__init__()

        # ── Window setup ─────────────────────────────────────────
        self.setWindowTitle("Zanuta Ramdisk Extractor — iPhone A12/A13")
        self.setMinimumSize(860, 520)
        self.resize(1024, 680)

        # ── Window icon ────────────────────────────────────────
        icon_path = Path(__file__).parent / "resources" / "icon.png"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        # ── Persistent state ─────────────────────────────────────
        self._settings = QSettings("RamdiskExtractor", "RamdiskExtractor")
        self._ipsw_path:  Optional[Path] = None
        self._output_dir: Optional[Path] = None
        self._ipsw_infos: List[IPSWInfo] = []
        self._path_to_row: dict[str, int] = {}
        self._path_to_progress: dict[str, QProgressBar] = {}

        # Remember last-used directories
        last_ipsw = self._settings.value("ipsw_path", "")
        last_out = self._settings.value("output_dir", "")
        if last_ipsw:
            self._ipsw_path = Path(last_ipsw)
        if last_out:
            self._output_dir = Path(last_out)

        # ── Worker ───────────────────────────────────────────────
        self._worker = Worker(self)
        self._connect_signals()

        # ── Build UI ─────────────────────────────────────────────
        self._setup_toolbar()
        self._setup_table()
        self._setup_log()
        self._setup_status_bar()
        self._setup_menu_bar()

    # ──────────────────────────────────────────────────────────────
    #  UI construction helpers
    # ──────────────────────────────────────────────────────────────

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        base = self.style().pixelMetric(QStyle.PM_ToolBarIconSize)
        tb.setIconSize(QSize(int(base * 1.4), int(base * 1.4)))

        self.addToolBar(tb)

        self._act_open_file = QAction(
            self.style().standardIcon(QStyle.SP_DirOpenIcon),
            "Open IPSW",
            self,
            shortcut="Ctrl+O",
            triggered=self._on_open_file,
        )
        tb.addAction(self._act_open_file)

        self._act_extract = QAction(
            self.style().standardIcon(QStyle.SP_MediaPlay),
            "Extract Ramdisk",
            self,
            shortcut="Ctrl+E",
            triggered=self._on_extract_all,
            enabled=False,
        )
        self._act_extract.setToolTip(
            "Extract the restore ramdisk from the loaded IPSW"
        )
        tb.addAction(self._act_extract)

        tb.addSeparator()

        self._act_stop = QAction(
            self.style().standardIcon(QStyle.SP_MediaStop),
            "Stop",
            self,
            shortcut="Escape",
            triggered=self._on_stop,
            enabled=False,
        )
        self._act_stop.setToolTip("Cancel the current extraction")
        tb.addAction(self._act_stop)

        self._act_clear = QAction(
            self.style().standardIcon(QStyle.SP_DialogCloseButton),
            "Clear Log",
            self,
            triggered=self._on_clear_log,
        )
        tb.addAction(self._act_clear)

        tb.addSeparator()

    def _setup_table(self) -> None:
        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels([
            "Device", "iOS Version", "Build", "Status", "Progress",
        ])
        self._table.setSelectionBehavior(
            self._table.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            self._table.SelectionMode.ExtendedSelection
        )
        self._table.setEditTriggers(
            self._table.EditTrigger.NoEditTriggers
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)   # enabled after scan completes
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        # Empty-state overlay — shown when table has zero rows
        device_list = "\n".join(
            f"  {DEVICE_NAMES.get(d, d)}  ({d})"
            for d in sorted(A12_A13_DEVICES)
        )
        self._empty_label = QLabel(
            f"Open an IPSW file to begin\n\n"
            f"Supported devices:\n{device_list}",
            self._table,
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet(
            "color: #999; font-size: 14px; background: transparent; padding: 20px;"
        )
        # Reposition label whenever the table geometry changes
        self._table.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._table and event.type() == event.Type.Resize:
            self._position_empty_label()
        return super().eventFilter(obj, event)

    def _position_empty_label(self) -> None:
        """Center the empty-state label within the table viewport."""
        vp = self._table.viewport()
        self._empty_label.setGeometry(vp.rect().adjusted(20, 40, -20, -40))

    def _setup_log(self) -> None:
        self._log_widget = QPlainTextEdit(self)
        self._log_widget.setReadOnly(True)
        self._log_widget.setMaximumBlockCount(2000)
        self._log_widget.setPlaceholderText("Log output will appear here …")

    def _setup_status_bar(self) -> None:
        self._status_total = QLabel("IPSWs: 0")
        self._status_ok = QLabel("Done: 0")
        self._status_skip = QLabel("Skip: 0")
        self._status_err = QLabel("Error: 0")

        sb = self.statusBar()
        sb.addWidget(self._status_total)
        self._status_msg = QLabel("Ready")
        self._status_msg.setContentsMargins(8, 0, 0, 0)
        sb.addWidget(self._status_msg, 1)  # stretch=1 → fills remaining space
        sb.addPermanentWidget(self._status_ok)
        sb.addPermanentWidget(self._status_skip)
        sb.addPermanentWidget(self._status_err)

        for lbl in (self._status_total, self._status_ok,
                    self._status_skip, self._status_err):
            lbl.setContentsMargins(6, 0, 6, 0)

    # ──────────────────────────────────────────────────────────────
    #  Layout
    # ──────────────────────────────────────────────────────────────

    def _build_central_widget(self) -> QWidget:
        """Stack the table and log vertically."""
        splitter = QSplitter(Qt.Vertical, self)
        splitter.addWidget(self._table)
        splitter.addWidget(self._log_widget)
        splitter.setStretchFactor(0, 2)  # table gets more space
        splitter.setStretchFactor(1, 1)

        w = QWidget(self)
        w.setLayout(QVBoxLayout(w))
        w.layout().setContentsMargins(0, 0, 0, 0)
        w.layout().addWidget(splitter)
        return w

    # ──────────────────────────────────────────────────────────────
    #  Worker signal wiring
    # ──────────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        w = self._worker
        w.log.connect(self._append_log)
        w.progress.connect(self._on_progress)
        w.file_progress.connect(self._on_file_progress)
        w.scan_item.connect(self._on_scan_item)
        w.scan_finished.connect(self._on_scan_finished)
        w.extraction_item.connect(self._on_extraction_item)
        w.extraction_finished.connect(self._on_extraction_finished)

    # ──────────────────────────────────────────────────────────────
    #  Toolbar action handlers
    # ──────────────────────────────────────────────────────────────

    def _on_open_file(self) -> None:
        start = str(self._ipsw_path.parent) if self._ipsw_path else ""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select an IPSW file",
            start,
            "IPSW files (*.ipsw *.IPSW);;All files (*)",
        )
        if not file_path:
            return

        self._ipsw_path = Path(file_path)
        self._settings.setValue("ipsw_path", str(self._ipsw_path))

        # Reset table & state
        self._reset_state()

        self._set_busy(True)
        self._status_msg.setText(f"Loading {self._ipsw_path.name} …")
        self._worker.start_scan(self._ipsw_path)

    def _on_extract_all(self) -> None:
        if not self._ipsw_infos:
            self._append_log("No ramdisks to extract.", "WARNING")
            return

        # Auto-determine output directory: same folder as IPSW, subfolder "ramdisks"
        suggested = (
            self._ipsw_path.parent / "ramdisks"
            if self._ipsw_path
            else Path.home() / "ramdisks"
        )
        self._output_dir = suggested
        self._settings.setValue("output_dir", str(self._output_dir))

        # Reset state per row
        for info in self._ipsw_infos:
            row = self._path_to_row.get(str(info.ipsw_path))
            if row is not None:
                self._set_row_status(row, ExtractionStatus.READY, "")

        self._set_busy(True, extracting=True)
        self._status_msg.setText(f"Extracting → {self._output_dir}")
        self._worker.start_extraction(self._ipsw_infos, self._output_dir)

    def _on_stop(self) -> None:
        self._worker.abort()
        self._act_stop.setEnabled(False)
        self._status_msg.setText("Stopping …")
        self._append_log("Stop requested …", "WARNING")

    def _on_clear_log(self) -> None:
        self._log_widget.clear()

    # ──────────────────────────────────────────────────────────────
    #  Table interaction
    # ──────────────────────────────────────────────────────────────

    def _on_row_double_clicked(self, row: int, col: int) -> None:
        """Extract a single ramdisk on double-click."""
        if self._worker.isRunning():
            self._append_log("Already running — wait or stop first.", "WARNING")
            return

        target = (
            self._ipsw_infos[row]
            if 0 <= row < len(self._ipsw_infos)
            else None
        )
        if target is None:
            return

        if not self._output_dir:
            suggested = (
                self._ipsw_path.parent / "ramdisks"
                if self._ipsw_path
                else Path.home() / "ramdisks"
            )
            self._output_dir = suggested
            self._settings.setValue("output_dir", str(self._output_dir))

        self._set_row_status(row, ExtractionStatus.EXTRACTING, "")
        self._set_busy(True, extracting=True)
        self._status_msg.setText("Extracting 1 ramdisk …")
        self._worker.start_extraction([target], self._output_dir)

    # ──────────────────────────────────────────────────────────────
    #  Worker slot handlers
    # ──────────────────────────────────────────────────────────────

    def _on_scan_item(self, info: IPSWInfo) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._empty_label.setVisible(False)

        item_device = QTableWidgetItem(info.display_name)
        item_device.setData(Qt.UserRole, str(info.ipsw_path))
        self._table.setItem(row, 0, item_device)
        self._table.setItem(row, 1, QTableWidgetItem(info.product_version))
        self._table.setItem(row, 2, QTableWidgetItem(info.product_build))
        item_status = QTableWidgetItem("Ready")
        item_status.setData(Qt.UserRole, ExtractionStatus.READY)
        self._table.setItem(row, 3, item_status)

        # Progress bar (hidden until extraction starts)
        pbar = QProgressBar(self._table)
        pbar.setMinimum(0)
        pbar.setMaximum(100)
        pbar.setValue(0)
        pbar.setVisible(False)
        self._table.setCellWidget(row, 4, pbar)

        self._path_to_row[str(info.ipsw_path)] = row
        self._path_to_progress[str(info.ipsw_path)] = pbar

    def _on_file_progress(self, ipsw_path: str, percent: int) -> None:
        """Update the per-file progress bar during extraction."""
        pbar = self._path_to_progress.get(ipsw_path)
        if pbar is None:
            return
        if percent == -1:
            pbar.setVisible(False)
        else:
            pbar.setVisible(True)
            pbar.setValue(percent)

    def _on_scan_finished(self, valid: List[IPSWInfo], _errors: list) -> None:
        # Log any errors from the scan phase
        for name, msg in _errors:
            self._append_log(f"{name}: {msg}", "ERROR")

        self._ipsw_infos = valid
        self._set_busy(False)

        if valid:
            self._status_msg.setText(
                f"{valid[0].display_name} — ramdisk ready to extract."
            )
            self._act_extract.setEnabled(True)
        else:
            self._append_log(
                "The selected IPSW does not contain a supported A12/A13 device.",
                "WARNING",
            )
            self._append_log(
                "Supported: iPhone XS/XR, XS Max, 11, 11 Pro, 11 Pro Max, SE (2nd gen)",
                "INFO",
            )
            self._status_msg.setText("Unsupported device — try another IPSW.")
            self._act_extract.setEnabled(False)
        self._update_status_bar()

    def _on_extraction_item(self, result: ExtractionResult) -> None:
        row = self._path_to_row.get(str(result.ipsw_info.ipsw_path))
        if row is None:
            return

        # Log structural inspection details on success
        insp = result.inspection
        if insp is not None and result.status == ExtractionStatus.SUCCESS:
            self._append_log(
                f"  Format: {insp.format_name}  |  {insp.details}",
                "SUCCESS" if insp.structure_valid else "WARNING",
            )
            if insp.filesystem:
                self._append_log(
                    f"  Filesystem: {insp.filesystem}",
                    "INFO",
                )

        self._set_row_status(row, result.status, result.message)

    def _on_extraction_finished(self, stats: Stats) -> None:
        self._set_busy(False)
        self._update_status_bar()

        if stats.completed < stats.total:
            msg = (
                f"Extraction cancelled — {stats.success} ramdisk(s) "
                f"extracted before stop."
            )
        elif stats.error:
            msg = (
                f"Done — {stats.success} OK, "
                f"{stats.skipped} skipped, "
                f"{stats.error} error(s)."
            )
        else:
            msg = (
                f"All done — {stats.success} ramdisk(s) extracted."
            )
        self._status_msg.setText(msg)
        self._append_log(msg, "INFO")

    def _on_progress(self, current: int, total: int) -> None:
        phase = "Loading" if self._worker.mode == "scan" else "Extracting"
        self._status_msg.setText(f"{phase} {current}/{total} …")
        self._update_status_bar()

    # ──────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────

    def _reset_state(self) -> None:
        self._worker.abort()
        if self._worker.isRunning():
            self._worker.wait(10000)
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._empty_label.setVisible(True)
        self._ipsw_infos.clear()
        self._path_to_row.clear()
        self._path_to_progress.clear()
        self._update_status_bar()
        self._act_extract.setEnabled(False)
        # Start fresh output selection on next extract
        self._output_dir = None

    def _set_busy(self, busy: bool, extracting: bool = False) -> None:
        self._act_open_file.setEnabled(not busy)
        self._act_extract.setEnabled(not busy and bool(self._ipsw_infos))
        self._act_stop.setEnabled(extracting)

        if busy:
            self._table.setCursor(Qt.WaitCursor)
        else:
            self._table.setCursor(Qt.ArrowCursor)

    def _set_row_status(
        self, row: int, status: ExtractionStatus, message: str = "",
    ) -> None:
        label = _STATUS_LABELS.get(status, str(status))
        color = _STATUS_COLORS.get(status, QColor("#1f2328"))

        item_status = self._table.item(row, 3) or QTableWidgetItem()
        item_status.setText(label)
        item_status.setForeground(color)
        item_status.setData(Qt.UserRole, status)
        self._table.setItem(row, 3, item_status)

        # Column 4 — Progress: bar durante EXTRACTING, texto nos terminais
        pbar = self._table.cellWidget(row, 4)

        if status == ExtractionStatus.SUCCESS:
            if pbar is not None:
                self._table.removeCellWidget(row, 4)
            item_progress = QTableWidgetItem("100%")
            item_progress.setTextAlignment(Qt.AlignCenter)
            item_progress.setForeground(color)
            self._table.setItem(row, 4, item_progress)

        elif status == ExtractionStatus.ERROR:
            if pbar is not None:
                self._table.removeCellWidget(row, 4)
            item_progress = QTableWidgetItem("\u2014")
            item_progress.setTextAlignment(Qt.AlignCenter)
            item_progress.setForeground(color)
            self._table.setItem(row, 4, item_progress)

        elif status == ExtractionStatus.SKIPPED:
            if pbar is not None:
                self._table.removeCellWidget(row, 4)
            item_progress = QTableWidgetItem("\u2014")
            item_progress.setTextAlignment(Qt.AlignCenter)
            item_progress.setForeground(color)
            self._table.setItem(row, 4, item_progress)

        elif status == ExtractionStatus.EXTRACTING:
            if pbar is not None:
                pbar.setValue(0)
                pbar.setVisible(True)
                self._table.setCellWidget(row, 4, pbar)
            # Do NOT use setItem — the bar is the cell widget

        # READY / outros: manter o estado actual (barra escondida)

    def _update_status_bar(self) -> None:
        ok = sum(
            1 for r in range(self._table.rowCount())
            if self._table.item(r, 3)
            and self._table.item(r, 3).data(Qt.UserRole) == ExtractionStatus.SUCCESS
        )
        skip = sum(
            1 for r in range(self._table.rowCount())
            if self._table.item(r, 3)
            and self._table.item(r, 3).data(Qt.UserRole) == ExtractionStatus.SKIPPED
        )
        err = sum(
            1 for r in range(self._table.rowCount())
            if self._table.item(r, 3)
            and self._table.item(r, 3).data(Qt.UserRole) == ExtractionStatus.ERROR
        )
        self._status_total.setText(f"IPSWs: {self._table.rowCount()}")
        self._status_ok.setText(f"Done: {ok}")
        self._status_skip.setText(f"Skip: {skip}")
        self._status_err.setText(f"Error: {err}")

    # ──────────────────────────────────────────────────────────────
    #  Log (coloured, with timestamp)
    # ──────────────────────────────────────────────────────────────

    def _append_log(self, message: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        short = {
            "SUCCESS": " OK ",
            "INFO":    "INFO",
            "WARNING": " WRN ",
            "ERROR":   " ERR ",
        }.get(level, "----")

        cursor = self._log_widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        fmt.setForeground(_LOG_COLORS.get(level, _LOG_COLORS["INFO"]))
        cursor.setCharFormat(fmt)
        cursor.insertText(f"[{ts}] {short}  {message}\n")

        self._log_widget.setTextCursor(cursor)

        # Only auto-scroll if the user is already at the bottom
        sb = self._log_widget.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        if at_bottom:
            self._log_widget.ensureCursorVisible()

    # ──────────────────────────────────────────────────────────────────────
    #  Help menu / About
    # ──────────────────────────────────────────────────────────────────────

    def _setup_menu_bar(self) -> None:
        """Create the menu bar with the Help menu."""
        mb = self.menuBar()

        help_menu = mb.addMenu("Help")
        about_action = help_menu.addAction("About Zanuta Ramdisk Extractor")
        about_action.triggered.connect(self._show_about)

    def _show_about(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.about(
            self,
            "About Zanuta Ramdisk Extractor",
            ("<b>Zanuta Ramdisk Extractor v{}</b><br><br>"
             "Developed by TimoCamada<br><br>"
             "Compatible with iPhone A12/A13 devices.").format(TOOL_VERSION),
        )


# ──────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("RamdiskExtractor")
    app.setOrganizationName("RamdiskExtractor")

    # Fusion style gives a consistent look across platforms
    app.setStyle("Fusion")

    win = MainWindow()
    win.setCentralWidget(win._build_central_widget())
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
