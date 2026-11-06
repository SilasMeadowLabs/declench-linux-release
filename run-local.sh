#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    if [[ -x "${SCRIPT_DIR}/../venv/bin/python" ]]; then
        "${SCRIPT_DIR}/../venv/bin/python" -m venv --system-site-packages "${VENV_DIR}"
    else
        echo "Error: create ${VENV_DIR} with --system-site-packages first." >&2
        exit 1
    fi
fi

# Register user-local icon, .desktop entry, and declench:// URI handler so GNOME Shell
# shows the official Declench icon and routes browser sign-in callbacks back to the app.
if [[ "${1:-}" != "--version" && "${1:-}" != "-h" && "${1:-}" != "--help" ]]; then
    USER_DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}"
    if mkdir -p "${USER_DATA_DIR}/icons/hicolor" "${USER_DATA_DIR}/applications" 2>/dev/null; then
        cp -rf "${SCRIPT_DIR}/data/icons/hicolor/." "${USER_DATA_DIR}/icons/hicolor/" 2>/dev/null || true
        sed \
            -e "s|@APP_ID@|app.declench.linux|g" \
            -e "s|Exec=declench-linux|Exec=${SCRIPT_DIR}/run-local.sh|g" \
            "${SCRIPT_DIR}/data/app.declench.linux.desktop.in" \
            > "${USER_DATA_DIR}/applications/app.declench.linux.desktop" 2>/dev/null || true

        if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
            gtk4-update-icon-cache -q -t -f "${USER_DATA_DIR}/icons/hicolor" 2>/dev/null || true
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q "${USER_DATA_DIR}/applications" 2>/dev/null || true
        fi
        if command -v xdg-mime >/dev/null 2>&1; then
            xdg-mime default app.declench.linux.desktop x-scheme-handler/declench 2>/dev/null || true
        fi
    fi
fi

if [[ "${1:-}" == "--restart" ]]; then
    shift
    if command -v gdbus >/dev/null 2>&1; then
        gdbus call --session \
            --dest app.declench.linux \
            --object-path /app/declench/linux \
            --method org.gtk.Actions.Activate \
            "quit" "[]" "{}" >/dev/null 2>&1 || true
        sleep 0.25
    fi
fi

export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${VENV_DIR}/bin/python" -m declench_gnome "$@"
