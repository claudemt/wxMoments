#!/usr/bin/env bash
# wxMoments launcher for macOS (manual db_key required)
set -e

cd "$(dirname "$0")"

if [ ! -f "config/config.json" ]; then
  cp "config/config.example.json" "config/config.json"
fi
mkdir -p "runtime"

PYTHON_BIN=""
if [ -x "runtime/.venv/bin/python" ]; then
  PYTHON_BIN="runtime/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python 3 not found. Please install Python 3."
  read -r -p "Press Enter to exit..."
  exit 1
fi

if [ ! -x "runtime/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "runtime/.venv"
fi
PYTHON_BIN="runtime/.venv/bin/python"

echo "[15%] Installing or repairing dependencies..."
"$PYTHON_BIN" -m ensurepip --upgrade >"runtime/install.log" 2>&1 || {
  echo "Dependency bootstrap failed. See: runtime/install.log"
  read -r -p "Press Enter to exit..."
  exit 1
}
if [ -d "wheels" ]; then
  "$PYTHON_BIN" -m pip install --no-index --find-links="wheels" --disable-pip-version-check -r "config/requirements.txt" >>"runtime/install.log" 2>&1 ||
    "$PYTHON_BIN" -m pip install --find-links="wheels" --disable-pip-version-check -r "config/requirements.txt" >>"runtime/install.log" 2>&1
else
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "config/requirements.txt" >>"runtime/install.log" 2>&1
fi
"$PYTHON_BIN" -m pip check >>"runtime/install.log" 2>&1 || {
  echo "Dependency installation failed. See: runtime/install.log"
  read -r -p "Press Enter to exit..."
  exit 1
}

"$PYTHON_BIN" "src/wxmoments.py" "$@"
if [ "$#" -eq 0 ]; then
  read -r -p "Press Enter to exit..."
fi
