#!/usr/bin/env bash
set -euo pipefail

app_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_command=${PYTHON:-python3}

if ! command -v "$python_command" >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi
if ! "$python_command" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

echo "Creating Open6009 environment in $app_dir/.venv"
"$python_command" -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --upgrade pip
"$app_dir/.venv/bin/python" -m pip install "$app_dir"

install -Dm644 "$app_dir/packaging/open6009.svg" \
  "$HOME/.local/share/icons/hicolor/scalable/apps/open6009.svg"
mkdir -p "$HOME/.local/share/applications"
desktop_tmp=$(mktemp)
trap 'rm -f "$desktop_tmp"' EXIT
escaped_dir=${app_dir//&/\\&}
sed "s&@APP_DIR@&$escaped_dir&g" "$app_dir/packaging/open6009.desktop.in" >"$desktop_tmp"
install -m644 "$desktop_tmp" "$HOME/.local/share/applications/open6009.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null || true
fi

echo
echo "Open6009 is installed. Launch it from your application menu or run:"
echo "  $app_dir/run-open6009"
echo
echo "If USB access is denied, install packaging/70-open6009.rules as described in README.md."
