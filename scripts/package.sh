#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  Zanuta Ramdisk Extractor — create distributable source package
#  Usage:  scripts/package.sh
#  Output: dist/zanuta-ramdisk-extractor-<version>.zip
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="zanuta-ramdisk-extractor"
VERSION="$(grep 'TOOL_VERSION' models.py | grep -oP '"\K[^"]+')"
OUTFILE="${ROOT}/dist/${APP_NAME}-${VERSION}.zip"
TMPDIR="$(mktemp -d "/tmp/${APP_NAME}-package-XXXXXX")"
STAGEDIR="${TMPDIR}/${APP_NAME}-${VERSION}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Zanuta Ramdisk Extractor v${VERSION} — Source Package"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

mkdir -p "${STAGEDIR}"
echo "  Staging files …"

# Copy all .py files from root
cp -a "${ROOT}"/*.py "${STAGEDIR}/"

# Copy config / docs
for f in Makefile pyproject.toml requirements.txt requirements-dev.txt README.md; do
    [ -f "${ROOT}/${f}" ] && cp -a "${ROOT}/${f}" "${STAGEDIR}/"
done

# Copy directories (resources, tests, scripts)
cp -a "${ROOT}/resources" "${STAGEDIR}/"
cp -a "${ROOT}/scripts" "${STAGEDIR}/"

# Copy tests — explicitly list files to avoid junk
mkdir -p "${STAGEDIR}/tests/fixtures"
for f in "${ROOT}"/tests/test_*.py "${ROOT}"/tests/__init__.py; do
    [ -f "$f" ] && cp -a "$f" "${STAGEDIR}/tests/"
done
[ -f "${ROOT}/tests/fixtures/BuildManifest.plist" ] && \
    cp -a "${ROOT}/tests/fixtures/BuildManifest.plist" "${STAGEDIR}/tests/fixtures/"

# ── Aggressive cleanup of unwanted artifacts ─────────────────────────
echo "  Cleaning up …"
find "${STAGEDIR}" -type d \( \
    -name "__pycache__" -o \
    -name "venv" -o \
    -name ".venv" -o \
    -name "_tmp_*" -o \
    -name ".git" -o \
    -name ".github" -o \
    -name ".agents" -o \
    -name ".codex" \
\) -exec rm -rf {} + 2>/dev/null || true

find "${STAGEDIR}" -type f \( \
    -name "*.pyc" -o \
    -name "*.spec" -o \
    -name ".gitignore" -o \
    -name "*.egg-info" \
\) -delete 2>/dev/null || true

# ── Create archive ──────────────────────────────────────────────────
mkdir -p "${ROOT}/dist"
rm -f "${OUTFILE}"

echo "  Compressing …"
if command -v 7z &>/dev/null; then
    cd "${TMPDIR}"
    7z a -tzip -mx=9 "${OUTFILE}" "${APP_NAME}-${VERSION}/" >/dev/null
elif command -v zip &>/dev/null; then
    cd "${TMPDIR}"
    zip -r -q -9 "${OUTFILE}" "${APP_NAME}-${VERSION}/"
else
    OUTFILE="${ROOT}/dist/${APP_NAME}-${VERSION}.tar.gz"
    cd "${TMPDIR}"
    tar czf "${OUTFILE}" "${APP_NAME}-${VERSION}/"
fi

rm -rf "${TMPDIR}"

SIZE=$(du -h "${OUTFILE}" 2>/dev/null | cut -f1)
FILES=$(unzip -l "${OUTFILE}" 2>/dev/null | tail -1 | awk '{print $2}')
echo ""
echo "  ✓ Package created:"
echo "    ${OUTFILE}"
echo "    ${SIZE}  —  ${FILES} files"
echo ""
echo "  Users can now:"
echo "    1. unzip ${APP_NAME}-${VERSION}.zip"
echo "    2. cd ${APP_NAME}-${VERSION}"
echo "    3. make setup   (or scripts\\build.bat on Windows)"
echo "    4. make build"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
