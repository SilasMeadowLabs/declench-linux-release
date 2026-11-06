# Declench for Linux (`app.declench.linux`)

Free, local-first **GTK 4 + Libadwaita** daytime jaw-posture coach for **GNOME Linux**, built by **Silas Meadow Labs**.

![Declench Coaching Dashboard](./screenshots/coach-dark.png)

## Highlights

- **100% Free Offline Local Mode**: Stores your adaptive schedule, streaks, and check-ins in local SQLite (`~/.local/share/declench/declench.sqlite3`) with zero account required.
- **Variable-Interval Daytime Nudges**: Randomized (`±Jitter`) check-ins around the passive **N** resting posture (*lips gently together, teeth slightly apart, tongue resting lightly behind upper front teeth*).
- **One-Click GNOME Notification Actions**: Log `Already in "N"`, `Caught & Reset`, `5s Urge`, or `Silence 1h` directly from the desktop notification banner.
- **8 Curated Themes & Stacked Analytics**: Includes 24-hour, 14-day, and 12-month posture charts plus CSV/JSON export.
- **Optional Declench Pro Cloud Sync**: Connect via browser (`PKCE` OAuth flow) to keep your streak and schedule synced across iPhone, Apple Watch, Android, and [declench.app](https://declench.app).

## Build & Install Flatpak Locally

```bash
flatpak install flathub org.flatpak.Builder org.gnome.Platform//51 org.gnome.Sdk//51
flatpak run --filesystem="$(pwd)" org.flatpak.Builder --user --install --force-clean build-flatpak app.declench.linux.json
flatpak run app.declench.linux
```

## Build & Validate Meson + AppStream Metadata

```bash
meson setup builddir --prefix=/app
ninja -C builddir
meson test -C builddir -v
```

## Run Locally from Source (Python Venv)

Create a local virtual environment with system `PyGObject` (`gi`) access and launch:

```bash
python3 -m venv --system-site-packages .venv
./run-local.sh
```

CLI flags:
- `--background`: Start hidden in background notification mode (for GNOME Startup Applications)
- `--trigger-now`: Fire an immediate desktop posture check-in notification
- `--restart`: Cleanly restart a running `app.declench.linux` instance
- `--version`: Print version and exit

## Run Unit Tests

```bash
.venv/bin/pip install pytest
PYTHONPATH=src .venv/bin/pytest tests -v
```

## License

Released under the [MIT License](./LICENSE). Copyright (c) 2026 Silas Meadow Labs.
