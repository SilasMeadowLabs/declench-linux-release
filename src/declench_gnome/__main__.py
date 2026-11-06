import argparse
import sys

from declench_gnome import APP_ID, APP_NAME, APP_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="declench-linux",
        description="Declench daytime jaw posture coach for GNOME Linux",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start quietly in background notification mode without opening the main window",
    )
    parser.add_argument(
        "--trigger-now",
        action="store_true",
        help="Send an immediate desktop posture check-in notification",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit",
    )
    args, gtk_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib

    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)

    from declench_gnome.app import DeclenchApplication

    app = DeclenchApplication(
        start_in_background=args.background,
        trigger_on_start=args.trigger_now,
    )
    return int(app.run([APP_ID, *gtk_args]))


if __name__ == "__main__":
    sys.exit(main())
