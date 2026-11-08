from pathlib import Path
import threading
from typing import Any, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from declench_gnome import APP_ID, APP_NAME, APP_VERSION, CANONICAL_WEB_URL  # noqa: E402
from declench_gnome.cloud_sync import DeclenchCloudClient  # noqa: E402
from declench_gnome.local_store import LocalDeclenchStore  # noqa: E402
from declench_gnome.themes import build_gtk_css, get_theme  # noqa: E402
from declench_gnome.window import DeclenchWindow  # noqa: E402


ACTIVE_NOTIFICATION_ID = "declench-active-nudge"


class DeclenchApplication(Adw.Application):
    """GNOME Adw.Application running the local SQLite scheduler and desktop notifications."""

    def __init__(
        self,
        *,
        start_in_background: bool = False,
        trigger_on_start: bool = False,
        store: Optional[LocalDeclenchStore] = None,
        cloud: Optional[DeclenchCloudClient] = None,
    ) -> None:
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.start_in_background = start_in_background
        self.trigger_on_start = trigger_on_start
        self.store = store or LocalDeclenchStore()
        self.cloud = cloud or DeclenchCloudClient()
        self.window: Optional[DeclenchWindow] = None
        self._css_provider = Gtk.CssProvider()
        self._scheduler_source_id: Optional[int] = None
        self._held_for_background = False
        self._tick_count = 0

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        display = Gdk.Display.get_default()
        if display is not None:
            local_icons = Path(__file__).resolve().parents[2] / "data" / "icons"
            if local_icons.is_dir():
                Gtk.IconTheme.get_for_display(display).add_search_path(str(local_icons))
            Gtk.Window.set_default_icon_name(APP_ID)
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self.apply_theme(self.store.get_setting("theme", "midnight"))
        self._register_actions()

        # Hold application alive so closing the main window keeps daytime nudges active
        if self.store.get_setting("run_in_background", "1") == "1" or self.start_in_background:
            self.hold()
            self._held_for_background = True

        self._scheduler_source_id = GLib.timeout_add_seconds(5, self._on_scheduler_tick)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = DeclenchWindow(application=self, store=self.store, cloud=self.cloud)

        if self.start_in_background:
            self.start_in_background = False
        else:
            self.window.present()
            self.window.refresh_all()

        if self.trigger_on_start:
            self.trigger_on_start = False
            self.trigger_nudge_and_notify()

    def do_open(self, files: list[Any], _n_files: int, _hint: str) -> None:
        """Receive `declench://auth-callback?...` deep links forwarded from the system browser."""
        if self.window is None:
            self.window = DeclenchWindow(application=self, store=self.store, cloud=self.cloud)
        self.window.present()
        self.window.view_stack.set_visible_child_name("everywhere")

        for gfile in files:
            uri = gfile.get_uri() if hasattr(gfile, "get_uri") else str(gfile)
            if uri and uri.lower().startswith("declench://"):
                self.window.handle_auth_callback_uri(uri)
                break

    def apply_theme(self, theme_key: str) -> None:
        palette = get_theme(theme_key)
        style_mgr = Adw.StyleManager.get_default()
        style_mgr.set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if palette.is_light else Adw.ColorScheme.FORCE_DARK
        )
        css = build_gtk_css(palette)
        self._css_provider.load_from_string(css)

    # --- Gio Actions & Interactive GNOME Notification Handlers ---

    def _register_actions(self) -> None:
        self._add_simple_action("quit", self._on_quit)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

        self._add_simple_action("about", self._on_about)
        self._add_simple_action("trigger-now", lambda *_: self.trigger_nudge_and_notify())
        self._add_simple_action(
            "trigger-exercise", lambda *_: self.trigger_nudge_and_notify(force_exercise=True)
        )
        self._add_simple_action("open-window", self._on_open_window)
        self._add_simple_action("silence-1h", self._on_notification_silence_1h)

        checkin_action = Gio.SimpleAction.new(
            "checkin-outcome", GLib.VariantType.new("(is)")
        )
        checkin_action.connect("activate", self._on_notification_checkin)
        self.add_action(checkin_action)

    def _add_simple_action(self, name: str, callback: Any) -> None:
        act = Gio.SimpleAction.new(name, None)
        act.connect("activate", callback)
        self.add_action(act)

    def _on_scheduler_tick(self) -> bool:
        self._tick_count += 1
        if not self.cloud.is_signed_in or self.cloud.is_this_device_enabled:
            fired = self.store.due_tick()
            if fired is not None:
                self._send_desktop_notification(fired)
                if self.window is not None and self.window.get_visible():
                    self.window.refresh_all()

        # Every 60s (12 x 5s ticks), pull fresh mobile/web check-ins when signed in
        if self._tick_count % 12 == 0 and self.cloud.is_signed_in:
            if self.window is not None:
                self.window.trigger_background_cloud_sync(silent=True)
            else:
                threading.Thread(
                    target=self._headless_cloud_sync, daemon=True
                ).start()
        return True

    def _headless_cloud_sync(self) -> None:
        try:
            self.cloud.sync_with_local_store(self.store)
        except Exception:
            pass

    def trigger_nudge_and_notify(self, force_exercise: bool = False) -> None:
        fired = self.store.trigger_now(
            force_exercise=force_exercise,
            trigger_source="manual",
        )
        self._send_desktop_notification(fired)
        if self.window is not None:
            self.window.refresh_all()
            self.window.show_toast("Sent desktop posture check-in")

    def _send_desktop_notification(self, log_entry: dict[str, Any]) -> None:
        notif = Gio.Notification.new(log_entry["title"])
        notif.set_body(log_entry["message"])
        notif.set_icon(Gio.ThemedIcon.new(APP_ID))
        notif.set_default_action("app.open-window")
        notif.set_priority(Gio.NotificationPriority.NORMAL)

        log_id = int(log_entry["id"])
        notif.add_button_with_target(
            'Already in "N"',
            "app.checkin-outcome",
            GLib.Variant("(is)", (log_id, "already_relaxed")),
        )
        notif.add_button_with_target(
            "Caught & Reset",
            "app.checkin-outcome",
            GLib.Variant("(is)", (log_id, "reset_now")),
        )
        notif.add_button_with_target(
            "5s Urge",
            "app.checkin-outcome",
            GLib.Variant("(is)", (log_id, "urge_resisted")),
        )
        notif.add_button("Silence 1h", "app.silence-1h")

        # Fixed ID replaces any previous unread Declench banner cleanly
        self.send_notification(ACTIVE_NOTIFICATION_ID, notif)

    def withdraw_active_nudge_banner(self) -> None:
        self.withdraw_notification(ACTIVE_NOTIFICATION_ID)

    def _on_notification_checkin(
        self, _action: Gio.SimpleAction, parameter: GLib.Variant
    ) -> None:
        log_id, outcome = parameter.unpack()
        try:
            self.store.record_feedback(int(log_id), str(outcome))
        except ValueError:
            pass
        self.withdraw_active_nudge_banner()
        if self.window is not None:
            if self.window.get_visible():
                self.window.refresh_all()
            if self.cloud.is_signed_in:
                self.window.trigger_background_cloud_sync(silent=True)

    def _on_notification_silence_1h(self, *_args: Any) -> None:
        self.store.pause_program(mode="minutes", minutes=60)
        self.withdraw_active_nudge_banner()
        if self.window is not None:
            if self.window.get_visible():
                self.window.refresh_all()
            if self.cloud.is_signed_in:
                self.window._run_cloud_mutation(
                    lambda: self.cloud.push_pause(self.store, "minutes", 60)
                )

    def _on_open_window(self, *_args: Any) -> None:
        if self.window is None:
            self.window = DeclenchWindow(application=self, store=self.store, cloud=self.cloud)
        self.window.present()
        self.window.refresh_all()

    def _on_about(self, *_args: Any) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Silas Meadow Labs",
            version=APP_VERSION,
            website=CANONICAL_WEB_URL,
            issue_url="https://github.com/SilasMeadowLabs/declench-linux-release/issues",
            license_type=Gtk.License.MIT_X11,
            comments=(
                "Free local-first daytime jaw posture coach for GNOME Linux, "
                "with optional Declench Pro cloud sync for iPhone, Apple Watch, Android, and Web."
            ),
        )
        about.present(self.window)

    def _on_quit(self, *_args: Any) -> None:
        if self._scheduler_source_id is not None:
            GLib.source_remove(self._scheduler_source_id)
            self._scheduler_source_id = None
        if self._held_for_background:
            self.release()
            self._held_for_background = False
        self.quit()

