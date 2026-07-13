#!/usr/bin/env python3
"""
Build a standalone executable of Ramdisk Extractor via PyInstaller.

Usage::

    pip install -r requirements-dev.txt
    python build.py

The resulting executable will be placed in ``dist/``.

Platform notes
--------------
* **macOS:** creates a ``.app`` bundle.  Pass ``--icon app.icns`` to get a
  custom icon (or place one at ``resources/icon.icns``).
* **Windows:** creates a ``.exe``.  Pass ``--icon app.ico`` to get a
  custom icon (or place one at ``resources/icon.ico``).
* **Linux:** creates a regular binary that can be run from the terminal.
"""

from __future__ import annotations

from os import environ
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def main() -> None:
    # ── Sanity checks ────────────────────────────────────────────
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed.")
        print("  pip install -r requirements-dev.txt")
        sys.exit(1)

    system = platform.system()
    print(f"Building Ramdisk Extractor for {system} …")

    # ── Clean previous builds ────────────────────────────────────
    for d in [DIST, BUILD]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  removed {d}")

    # ── Common arguments ─────────────────────────────────────────
    args = [
        sys.executable or "python3",
        "-m",
        "PyInstaller",
        "--clean",
        "--name",
        "ZanutaRamdiskExtractor",
    ]

    # Windowed (no console) or console mode
    if system == "Darwin":
        args.append("--windowed")
    elif system == "Windows":
        args.append("--windowed")
    # Linux: keep console for now (use --noconsole in args to suppress)

    # One-file bundles
    args.append("--onefile")

    # Icon (auto-detect)
    icon = _find_icon(system)
    if icon:
        args.extend(["--icon", str(icon)])
        print(f"  using icon: {icon.name}")
    else:
        print("  no icon found — skipping")

    # macOS bundle metadata
    if system == "Darwin":
        args.extend([
            "--osx-bundle-identifier",
            "com.ramdiskextractor.app",
        ])
        # CI builds usually don't have signing certs — skip signing
        if environ.get("SKIP_CODESIGN", "").strip() in ("1", "true", "yes"):
            args.append("--nosign")
            print("  codesign skipped (SKIP_CODESIGN=1)")

    # Collect PySide6 data files (plugins, translations, etc.)
    # NOTE: the path separator in --add-data is platform-specific
    # (Unix:  Linux/macOS  →  ":")
    # (Windows:              ";")
    import PySide6  # noqa: F811
    pyside_dir = Path(PySide6.__file__).parent
    if (pyside_dir / "plugins").is_dir():
        sep = ";" if system == "Windows" else ":"
        args.extend(["--add-data", f"{pyside_dir / 'plugins'}{sep}PySide6/plugins"])

    # Entry point
    args.append(str(ROOT / "app.py"))

    # ── Run PyInstaller ──────────────────────────────────────────
    print(f"\nRunning: {' '.join(str(a) for a in args)}\n")

    # Headless Linux: Qt needs a platform backend even during build
    env = None
    if system == "Linux":
        env = {**environ, "QT_QPA_PLATFORM": "offscreen"}

    result = subprocess.run(args, cwd=ROOT, env=env)
    if result.returncode != 0:
        print(f"\nBuild failed with exit code {result.returncode}.")
        sys.exit(result.returncode)

    # ── Post-build info ──────────────────────────────────────────
    print()
    if system == "Darwin":
        bundle = DIST / "ZanutaRamdiskExtractor.app"
        if bundle.is_dir():
            print(f"✓ Bundle created: {bundle}")
            print(f"  Size: {_dir_size(bundle) / 1_000_000:.0f} MB")
    else:
        exe = DIST / ("ZanutaRamdiskExtractor.exe" if system == "Windows" else "ZanutaRamdiskExtractor")
        if exe.is_file():
            print(f"✓ Executable created: {exe}")
            print(f"  Size: {exe.stat().st_size / 1_000_000:.0f} MB")

    print("Done.")


# ── Helpers ──────────────────────────────────────────────────────────

def _find_icon(system: str) -> Path | None:
    """Look for a platform-appropriate icon in ``resources/``."""
    resources = ROOT / "resources"
    if not resources.is_dir():
        return None

    ext = ".icns" if system == "Darwin" else ".ico" if system == "Windows" else ".png"
    candidates = sorted(resources.glob(f"*{ext}"))
    return candidates[0] if candidates else None


def _dir_size(path: Path) -> int:
    """Recursive directory size in bytes."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


if __name__ == "__main__":
    main()
