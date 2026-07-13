@echo off
REM ──────────────────────────────────────────────────────────────────────────
REM  Zanuta Ramdisk Extractor — Windows setup + build script
REM  Double-click or run from cmd:
REM      scripts\build.bat
REM ──────────────────────────────────────────────────────────────────────────

title Zanuta Ramdisk Extractor — Setup & Build
cd /d "%~dp0.."
setlocal enabledelayedexpansion

echo.
echo ============================================
echo   Zanuta Ramdisk Extractor — Windows Build
echo ============================================
echo.

REM ── Find Python ─────────────────────────────────────────────────────
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python ^>= 3.11 from python.org
    pause
    exit /b 1
)

REM ── Create venv ─────────────────────────────────────────────────────
if not exist "venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment ...
    python -m venv venv
) else (
    echo [1/4] Virtual environment already exists.
)

REM ── Install dependencies ────────────────────────────────────────────
echo [2/4] Installing dependencies ...
call venv\Scripts\pip install --upgrade pip >nul 2>&1
call venv\Scripts\pip install -r requirements.txt
call venv\Scripts\pip install -r requirements-dev.txt
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

REM ── Run tests ────────────────────────────────────────────────────────
echo [3/4] Running tests ...
call venv\Scripts\python -m unittest discover -s tests -v
echo.

REM ── Build executable ────────────────────────────────────────────────
echo [4/4] Building standalone executable (this may take a few minutes) ...
call venv\Scripts\python build.py
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo   SUCCESS! Executable created in:
    echo   %CD%\dist\ZanutaRamdiskExtractor.exe
    echo ============================================
) else (
    echo [ERROR] Build failed.
)

echo.
pause
