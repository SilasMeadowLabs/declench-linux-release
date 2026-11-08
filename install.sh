#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--flatpak" ]]; then
    echo "Installing Declench as a user Flatpak (app.declench.linux)..."
    if command -v flatpak-builder >/dev/null 2>&1; then
        flatpak-builder --user --install --force-clean "${SCRIPT_DIR}/build-flatpak" "${SCRIPT_DIR}/app.declench.linux.json"
    elif command -v flatpak >/dev/null 2>&1; then
        flatpak install -y flathub org.flatpak.Builder org.gnome.Platform//51 org.gnome.Sdk//51
        flatpak run --filesystem="${SCRIPT_DIR}" org.flatpak.Builder --user --install --force-clean "${SCRIPT_DIR}/build-flatpak" "${SCRIPT_DIR}/app.declench.linux.json"
    else
        echo "Error: flatpak is not installed on this system." >&2
        exit 1
    fi
    echo "Done! Launch Declench from your GNOME app grid or run: flatpak run app.declench.linux"
    exit 0
fi

echo "Setting up Declench for Linux (local GNOME desktop installation)..."
if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    python3 -m venv --system-site-packages "${SCRIPT_DIR}/.venv"
fi

# Verify version and register desktop icon, .desktop launcher, and declench:// URI handler
"${SCRIPT_DIR}/run-local.sh" --version >/dev/null

USER_DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}"
mkdir -p "${USER_DATA_DIR}/icons/hicolor" "${USER_DATA_DIR}/applications"
cp -rf "${SCRIPT_DIR}/data/icons/hicolor/." "${USER_DATA_DIR}/icons/hicolor/"
sed \
    -e "s|@APP_ID@|app.declench.linux|g" \
    -e "s|Exec=declench-linux|Exec=${SCRIPT_DIR}/run-local.sh|g" \
    "${SCRIPT_DIR}/data/app.declench.linux.desktop.in" \
    > "${USER_DATA_DIR}/applications/app.declench.linux.desktop"

if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache -q -t -f "${USER_DATA_DIR}/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "${USER_DATA_DIR}/applications" 2>/dev/null || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default app.declench.linux.desktop x-scheme-handler/declench 2>/dev/null || true
fi

echo ""
echo "Declench is installed!"
echo "  • Open 'Declench' from your GNOME Applications grid, or"
echo "  • Run '${SCRIPT_DIR}/run-local.sh' to launch now."

