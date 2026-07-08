# Zanuta Ramdisk Extractor
*by TimoCamada*

A fast, offline tool to extract restore ramdisks from iPhone A12/A13 IPSWs —
**no decryption or server calls needed**.

Supports iPhone XS, XR, 11, 11 Pro, 11 Pro Max, and SE (2nd gen).

## Quick start

### Run from source

```bash
cd ramdisk_extractor
python3 -m venv venv
source venv/bin/activate                 # venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py
```

### Build a standalone executable

```bash
pip install -r requirements-dev.txt
python build.py
```

The compiled binary lands in `dist/`:

| Platform | Output |
|----------|--------|
| macOS    | `dist/ZanutaRamdiskExtractor.app` (drag to Applications) |
| Windows  | `dist/ZanutaRamdiskExtractor.exe` |
| Linux    | `dist/ZanutaRamdiskExtractor` |

**Expected size:** 40–80 MB (PySide6 + Python interpreter bundled).

### Run automated tests

```bash
source venv/bin/activate
python test_all.py           # 54 tests, ~0.8s
python test_all.py -v        # verbose
```

### Command-line mode (no GUI)

```bash
python backend.py /path/to/ipsw/cache -o /path/to/output
python backend.py --dry-run /path/to/ipsw/cache      # preview only
python backend.py -vv /path/to/ipsw/cache            # debug verbosity
```

## Usage

1. Click **Open Folder** (or drag a folder into the window).
2. The tool recursively scans the folder for `.ipsw` files and parses each one.
3. Non-A12/A13 devices are ignored automatically.
4. Select **Extract All** (or double-click a single row).
5. Choose an output directory.
6. Wait — progress appears in the table and log panel.

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+O` | Open folder |
| `Ctrl+E` | Extract all |
| `Ctrl+D` | Set output directory |
| `Escape` | Stop extraction |

### Output structure

```
<output_base>/
  iPhone12,1/
    14.3/
      iPhone12,1_14.3_18C66_ramdisk.dmg
  iPhone12,3/
    14.4/
      iPhone12,3_14.4_18D52_ramdisk.dmg
```

Each DMG is validated by checking the Apple UDIF trailer signature (`koly`).
If the same ramdisk is extracted twice, the second one gets a `_1`, `_2`, …
suffix automatically — no silent overwrites.

### Supported devices

| Model       | Name                |
|-------------|---------------------|
| iPhone11,2  | iPhone XS           |
| iPhone11,4  | iPhone XS Max (China) |
| iPhone11,6  | iPhone XS Max       |
| iPhone11,8  | iPhone XR           |
| iPhone12,1  | iPhone 11           |
| iPhone12,3  | iPhone 11 Pro       |
| iPhone12,5  | iPhone 11 Pro Max   |
| iPhone12,8  | iPhone SE (2nd gen) |

## Project structure

```
ramdisk_extractor/
  app.py                # PySide6 GUI (toolbar, table, log, menus)
  backend.py            # Core logic: scan, parse, extract, validate
  models.py             # Data classes and constants
  worker.py             # QThread for background operations
  build.py              # PyInstaller build script
  test_all.py           # 54 automated tests (no GUI)
  requirements.txt      # Runtime dependencies
  requirements-dev.txt  # Build-time dependencies
  README.md
```

## License

This project is provided under the MIT License.  PySide6 is licensed
under the LGPL, so distribution of the compiled binary does not require
source disclosure of your own code.
