# Zanuta Ramdisk Extractor

Extract and validate **restore ramdisks** and **firmware components** (iBSS, iBEC, DeviceTree, KernelCache, SEP) from iPhone A12/A13 IPSW archives.

---

## Features

- **Ramdisk extraction** — streams the restore ramdisk DMG from the IPSW to disk with structural validation
- **Firmware components** — extracts iBSS, iBEC, DeviceTree, KernelCache, and SEP
- **macOS validation** (macOS only):
  - `hdiutil verify` — native DMG integrity verification
  - `img4tool --verify` — IMG4 signature verification for iBSS, iBEC, DeviceTree, SEP
  - `hdiutil attach -nomount` — confirms the ramdisk is mountable
- **Digest verification** — SHA-384 from BuildManifest.plist
- **Metadata sidecar** — JSON with device, firmware, digest, and validation results
- **Graphical interface** — PySide6 with IPSW table, colored log, and progress bar
- **Command line** — batch processing with `--dry-run`

---

## Requirements

- Python >= 3.11
- PySide6 >= 6.0 (runtime)
- _Optional (macOS):_ `hdiutil` (built-in), `img4tool` for signature verification

---

## Installation

```bash
git clone <repo>
cd ramdisk_extractor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### Graphical interface

```bash
python app.py
```

### Command line

```bash
python backend.py ~/Downloads/IPSWs -o ./ramdisks
python backend.py ~/Downloads/IPSWs --dry-run
```

---

## Standalone build

Generates a single executable (via PyInstaller) in `dist/`:

```bash
pip install -r requirements-dev.txt
python build.py
```

Output:

| Platform | Artifact |
|----------|----------|
| Linux    | `dist/ZanutaRamdiskExtractor` |
| macOS    | `dist/ZanutaRamdiskExtractor.app` |
| Windows  | `dist/ZanutaRamdiskExtractor.exe` |

### Universal source package

```bash
make package       # creates dist/zanuta-ramdisk-extractor-<version>.zip
```

The zip contains all source files, a `Makefile` (Linux/macOS), and `scripts/build.bat` (Windows). Users can build from source on any platform.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

---

## Project structure

```
ramdisk_extractor/
├── app.py              # PySide6 GUI
├── backend.py          # Facade + CLI entry point
├── build.py            # PyInstaller build script
├── extractor.py        # Extraction logic
├── models.py           # Data models
├── parser.py           # IPSW / BuildManifest parsing
├── scanner.py          # IPSW file discovery
├── validator.py        # DMG and component validation
├── worker.py           # Background worker thread for the GUI
├── Makefile            # Universal build targets
├── pyproject.toml      # Project metadata
├── scripts/
│   ├── build.bat       # Windows setup + build script
│   └── package.sh      # Source package generator
├── tests/              # Test suite (131 tests)
└── resources/          # App icons (icns, ico, png)
```

---

## CI/CD

GitHub Actions workflow (`.github/workflows/build.yml`) builds for all three platforms on every push to `main`, runs the full test suite, and attaches artifacts to releases.

---

## License

MIT
