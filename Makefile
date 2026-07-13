# ──────────────────────────────────────────────────────────────────────────────
#  Zanuta Ramdisk Extractor — universal Makefile
#  Targets:  setup | test | build | package | clean | all
#  Usage:    make setup   (one time)
#            make build   (after setup)
#            make package (creates distributable zip)
# ──────────────────────────────────────────────────────────────────────────────

.DEFAULT_GOAL := all

APP_NAME     := ZanutaRamdiskExtractor
VERSION      := $(shell grep TOOL_VERSION models.py 2>/dev/null | cut -d'"' -f2)
PYTHON       ?= python3
VENV         ?= venv
VENV_BIN     := $(VENV)/bin
PIP          := $(VENV_BIN)/pip
PYTHON_VENV  := $(VENV_BIN)/python
PLATFORM     := $(shell uname -s 2>/dev/null || echo Windows)

# Detect Windows (no uname)
ifeq ($(OS),Windows_NT)
    VENV_BIN    := $(VENV)/Scripts
    PIP         := $(VENV_BIN)/pip
    PYTHON_VENV := $(VENV_BIN)/python
    RM         := del /q /s
    SEP        := \\
else
    RM         := rm -rf
    SEP        := /
endif

# ─── Setup ─────────────────────────────────────────────────────────────

setup: $(VENV)/pyvenv.cfg

$(VENV)/pyvenv.cfg:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt
	@echo ""
	@echo "✓ Virtual environment ready. Run 'make test' or 'make build'."

# ─── Tests ─────────────────────────────────────────────────────────────

.PHONY: test
test: $(VENV)/pyvenv.cfg
	$(PYTHON_VENV) -m unittest discover -s tests -v

.PHONY: test-quick
test-quick: $(VENV)/pyvenv.cfg
	$(PYTHON_VENV) -m unittest discover -s tests -q

# ─── Build (standalone executable via PyInstaller) ────────────────────

.PHONY: build
build: $(VENV)/pyvenv.cfg
	$(PYTHON_VENV) build.py

# ─── Package (distributable source zip) ───────────────────────────────

.PHONY: package
package:
	@echo "Packaging Zanuta Ramdisk Extractor v$(VERSION) …"
	@scripts/package.sh

# ─── Clean ─────────────────────────────────────────────────────────────

.PHONY: clean
clean:
	$(RM) build dist *.spec
	$(RM) $(VENV) __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "✓ Cleaned."

# ─── All ────────────────────────────────────────────────────────────────

.PHONY: all
all: setup test build package
	@echo ""
	@echo "✓ All done. See dist/ for executables and zanuta-ramdisk-extractor-*.zip"

# ─── Help ──────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo "Zanuta Ramdisk Extractor — Makefile"
	@echo ""
	@echo "  make setup     Create venv and install dependencies"
	@echo "  make test      Run test suite"
	@echo "  make build     Build standalone executable (PyInstaller)"
	@echo "  make package   Create distributable source zip"
	@echo "  make clean     Remove build artifacts and venv"
	@echo "  make all       setup → test → build → package"
	@echo ""
	@echo "  PYTHON=python3.12  (override Python interpreter)"
