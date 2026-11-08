from pathlib import Path
import threading
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from declench_gnome.chart_widget import StackedTimeseriesChart  # noqa: E402
from declench_gnome.cloud_sync import (  # noqa: E402
    BILLING_WEB_URL,
    HOMEPAGE_MOBILE_URL,
    METHOD_WEB_URL,
    SIGNUP_TRIAL_URL,
    CloudSyncError,
    DeclenchCloudClient,
)
from declench_gnome.local_store import LocalDeclenchStore  # noqa: E402
from declench_gnome.schedule_math import interval_to_seconds  # noqa: E402
from declench_gnome.themes import THEMES, get_theme  # noqa: E402


class DeclenchWindow(Adw.ApplicationWindow):
    """Main GTK4 + Libadwaita window for Declench on GNOME Linux."""

    def __init__(
        self,
        application: Adw.Application,
        store: LocalDeclenchStore,
        cloud: DeclenchCloudClient,
    ) -> None:
        super().__init__(application=application, title="Declench")
        self.store = store
        self.cloud = cloud
        self.selected_granularity = "daily"
        self.history_outcome_filter = "all"
        self._updating_ui = False
        self._sync_lock = threading.Lock()
        self._sync_in_flight = False

        self.set_default_size(900, 760)
        self.set_size_request(380, 560)
        self.set_icon_name("app.declench.linux")
        self.add_css_class("declench-window")
        self.set_hide_on_close(self._background_enabled())

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        self.view_stack = Adw.ViewStack()

        switcher_title = Adw.ViewSwitcherTitle(stack=self.view_stack, title="Declench")
        header.set_title_widget(switcher_title)

        # Primary GNOME hamburger menu (About, Send Test Nudge, Quit Completely)
        menu = Gio.Menu()
        menu.append("Send Test Nudge", "app.trigger-now")
        menu.append("Send Jaw Stretch Prompt", "app.trigger-exercise")
        menu.append("About Declench", "app.about")
        menu.append("Quit Completely", "app.quit")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.view_stack)

        bottom_bar = Adw.ViewSwitcherBar(stack=self.view_stack)
        switcher_title.bind_property(
            "title-visible", bottom_bar, "reveal", GObject.BindingFlags.SYNC_CREATE
        )
        toolbar_view.add_bottom_bar(bottom_bar)

        self._build_coach_tab()
        self._build_schedule_tab()
        self._build_history_tab()
        self._build_method_tab()
        self._build_everywhere_tab()

        self.refresh_all()
        if self.cloud.is_signed_in:
            self.trigger_background_cloud_sync(silent=True, announce_imports=True)

    def _background_enabled(self) -> bool:
        return self.store.get_setting("run_in_background", "1") == "1"

    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_use_markup(False)
        self.toast_overlay.add_toast(toast)

    # --- Tab 1: Coach Dashboard ---

    def _build_coach_tab(self) -> None:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=16, margin_bottom=24, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(box)

        # Coaching status card
        coach_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        coach_card.add_css_class("declench-card")

        self.lbl_phase = Gtk.Label(xalign=0)
        self.lbl_phase.add_css_class("declench-badge")
        coach_card.append(self.lbl_phase)

        self.lbl_headline = Gtk.Label(xalign=0, wrap=True)
        self.lbl_headline.add_css_class("title-3")
        coach_card.append(self.lbl_headline)

        self.lbl_coach_note = Gtk.Label(xalign=0, wrap=True)
        self.lbl_coach_note.add_css_class("declench-muted")
        coach_card.append(self.lbl_coach_note)

        self.goal_bar = Gtk.ProgressBar(show_text=False, margin_top=4)
        coach_card.append(self.goal_bar)

        self.lbl_sched_status = Gtk.Label(xalign=0, wrap=True)
        self.lbl_sched_status.add_css_class("caption")
        coach_card.append(self.lbl_sched_status)

        box.append(coach_card)

        # 3 Metric Cards
        metrics_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.lbl_streak_val = self._make_metric_card(metrics_row, "Streak", "declench-metric-val")
        self.lbl_auto_val = self._make_metric_card(
            metrics_row, 'Already in "N"', "declench-metric-val-cyan"
        )
        self.lbl_urge_val = self._make_metric_card(
            metrics_row, "5s Urges", "declench-metric-val-amber"
        )
        box.append(metrics_row)

        # Quick Actions Card (+1 5s Urge + Silence/Resume + Test Nudge)
        actions_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        actions_card.add_css_class("declench-card")

        urge_btn = Gtk.Button(label="+1 5-Second Urge Resisted")
        urge_btn.add_css_class("declench-primary-cta")
        urge_btn.connect("clicked", self._on_quick_urge_clicked)
        actions_card.append(urge_btn)

        ctrl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        btn_s1h = Gtk.Button(label="Silence 1 Hour")
        btn_s1h.connect("clicked", lambda *_: self._on_pause("minutes", 60))
        ctrl_row.append(btn_s1h)

        btn_morning = Gtk.Button(label="Until Morning")
        btn_morning.connect("clicked", lambda *_: self._on_pause("until_morning", 0))
        ctrl_row.append(btn_morning)

        self.btn_resume = Gtk.Button(label="Resume Now")
        self.btn_resume.connect("clicked", self._on_resume_clicked)
        ctrl_row.append(self.btn_resume)

        btn_nudge = Gtk.Button(label="Nudge Now")
        btn_nudge.connect("clicked", lambda *_: self.get_application().trigger_nudge_and_notify())
        ctrl_row.append(btn_nudge)

        actions_card.append(ctrl_row)
        box.append(actions_card)

        # Stacked Bar Chart Card
        chart_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        chart_card.add_css_class("declench-card")

        chart_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chart_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        lbl_ct = Gtk.Label(label="Posture Check-In Analytics", xalign=0)
        lbl_ct.add_css_class("heading")
        self.lbl_chart_window = Gtk.Label(label="Last 14 Days", xalign=0)
        self.lbl_chart_window.add_css_class("caption")
        self.lbl_chart_window.add_css_class("declench-muted")
        chart_title_box.append(lbl_ct)
        chart_title_box.append(self.lbl_chart_window)
        chart_hdr.append(chart_title_box)

        gran_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for key, label in (("hourly", "24h"), ("daily", "14d"), ("monthly", "12m")):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._on_granularity_clicked, key)
            gran_box.append(b)
        chart_hdr.append(gran_box)
        chart_card.append(chart_hdr)

        self.chart = StackedTimeseriesChart()
        chart_card.append(self.chart)

        self.lbl_peak_tension = Gtk.Label(xalign=0, wrap=True)
        self.lbl_peak_tension.add_css_class("caption")
        self.lbl_peak_tension.add_css_class("declench-muted")
        chart_card.append(self.lbl_peak_tension)
        box.append(chart_card)

        # Recent Check-Ins + Clear Unanswered
        recent_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_rec = Gtk.Label(label="Recent Desk Nudges", xalign=0, hexpand=True)
        lbl_rec.add_css_class("heading")
        recent_hdr.append(lbl_rec)

        self.btn_clear_unanswered = Gtk.Button(label="Clear Unanswered")
        self.btn_clear_unanswered.connect("clicked", self._on_clear_unanswered)
        recent_hdr.append(self.btn_clear_unanswered)
        box.append(recent_hdr)

        self.recent_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.recent_list_box)

        # Subtle Dismissible "Declench on iPhone & Android" Discovery Banner
        self.promo_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.promo_banner.add_css_class("declench-pro-banner")
        promo_text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        promo_title = Gtk.Label(
            label="Away from your Linux desk? Use Declench on iPhone, Apple Watch & Android",
            xalign=0,
            wrap=True,
        )
        promo_title.add_css_class("heading")
        promo_sub = Gtk.Label(
            label="Linux desktop mode is 100% free forever. Start an optional 5-day free trial of Declench Pro to sync your streak and get 1-tap Lock Screen nudges on the go.",
            xalign=0,
            wrap=True,
        )
        promo_sub.add_css_class("caption")
        promo_sub.add_css_class("declench-muted")
        promo_text_box.append(promo_title)
        promo_text_box.append(promo_sub)
        self.promo_banner.append(promo_text_box)

        btn_see_mobile = Gtk.Button(label="See Mobile Sync", valign=Gtk.Align.CENTER)
        btn_see_mobile.connect(
            "clicked", lambda *_: self.view_stack.set_visible_child_name("everywhere")
        )
        self.promo_banner.append(btn_see_mobile)

        btn_dismiss_promo = Gtk.Button(
            icon_name="window-close-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
        )
        btn_dismiss_promo.connect("clicked", self._on_dismiss_promo)
        self.promo_banner.append(btn_dismiss_promo)
        box.append(self.promo_banner)

        self.view_stack.add_titled_with_icon(
            scroll, "coach", "Coach", "app.declench.linux-symbolic"
        )

    @staticmethod
    def _make_metric_card(parent: Gtk.Box, label: str, val_css: str) -> Gtk.Label:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("declench-card")
        val = Gtk.Label(label="0", xalign=0.5)
        val.add_css_class(val_css)
        cap = Gtk.Label(label=label, xalign=0.5)
        cap.add_css_class("caption")
        cap.add_css_class("declench-muted")
        card.append(val)
        card.append(cap)
        parent.append(card)
        return val

    # --- Tab 2: Schedule & Appearance Settings ---

    def _build_schedule_tab(self) -> None:
        page = Adw.PreferencesPage()

        # Cadence Group
        cad_group = Adw.PreferencesGroup(
            title="Daytime Polling Cadence",
            description="Randomized ±25% timing prevents sensory habituation while you code or read.",
        )
        self.row_random_mode = Adw.SwitchRow(
            title="Random Brain-Hack Mode (±Jitter)",
            subtitle="Vary each interval randomly so your brain notices every posture cue",
            use_markup=False,
        )
        cad_group.add(self.row_random_mode)

        self.spin_jitter = Adw.SpinRow.new_with_range(5, 55, 5)
        self.spin_jitter.set_use_markup(False)
        self.spin_jitter.set_title("Random Jitter Percentage (%)")
        cad_group.add(self.spin_jitter)

        self.spin_interval = Adw.SpinRow.new_with_range(1, 240, 5)
        self.spin_interval.set_use_markup(False)
        self.spin_interval.set_title("Starting Interval (Minutes)")
        cad_group.add(self.spin_interval)

        self.row_progressive = Adw.SwitchRow(
            title="Progressive & Adaptive Widening",
            subtitle='Automatically widen toward goal as Already in "N" check-ins climb',
            use_markup=False,
        )
        cad_group.add(self.row_progressive)

        self.spin_target = Adw.SpinRow.new_with_range(1, 360, 5)
        self.spin_target.set_use_markup(False)
        self.spin_target.set_title("Goal Interval (Minutes)")
        cad_group.add(self.spin_target)

        self.row_exercises = Adw.SwitchRow(
            title="Occasional Jaw Stretch & Cheek Massage Prompts",
            subtitle="Blend gentle jaw unwind prompts into regular nudges with zero extra alerts",
            use_markup=False,
        )
        cad_group.add(self.row_exercises)
        page.add(cad_group)

        # Quiet Hours & Desktop Behavior
        awake_group = Adw.PreferencesGroup(
            title="Awake Window &amp; GNOME Desktop Behavior",
            description="24-hour HH:MM local times protecting evening and sleep hours.",
        )
        self.entry_start = Adw.EntryRow(title="Morning Wake Time (HH:MM)", use_markup=False)
        self.entry_end = Adw.EntryRow(title="Evening Quiet Time (HH:MM)", use_markup=False)
        self.entry_tz = Adw.EntryRow(title="IANA Timezone", use_markup=False)
        self.row_sound = Adw.SwitchRow(
            title="Notification Sound",
            subtitle="Play standard desktop notification chime on posture checks",
            use_markup=False,
        )
        self.row_background = Adw.SwitchRow(
            title="Keep Running When Window Is Closed",
            subtitle="Continue daytime posture nudges quietly in the background",
            use_markup=False,
        )
        awake_group.add(self.entry_start)
        awake_group.add(self.entry_end)
        awake_group.add(self.entry_tz)
        awake_group.add(self.row_sound)
        awake_group.add(self.row_background)
        page.add(awake_group)

        # 8 Curated Themes
        theme_group = Adw.PreferencesGroup(
            title="Appearance Theme (8 Curated Palettes)",
            description="Included free on Linux desktop.",
        )
        theme_names = Gtk.StringList.new([f"{t.display_name} ({t.badge})" for t in THEMES])
        self.combo_theme = Adw.ComboRow(title="Color Theme", model=theme_names)
        self.combo_theme.connect("notify::selected", self._on_theme_changed)
        theme_group.add(self.combo_theme)
        page.add(theme_group)

        # Save / Reset Buttons
        act_group = Adw.PreferencesGroup()
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin_top=6)
        save_btn = Gtk.Button(label="Save Schedule Settings", hexpand=True)
        save_btn.add_css_class("declench-primary-cta")
        save_btn.connect("clicked", self._on_save_schedule)
        btn_box.append(save_btn)

        reset_btn = Gtk.Button(label="Reset Interval")
        reset_btn.connect("clicked", self._on_reset_interval)
        btn_box.append(reset_btn)

        defaults_btn = Gtk.Button(label="Restore Defaults")
        defaults_btn.connect("clicked", self._on_restore_defaults)
        btn_box.append(defaults_btn)

        act_group.add(btn_box)
        page.add(act_group)

        self.view_stack.add_titled_with_icon(
            page, "schedule", "Schedule", "alarm-symbolic"
        )

    # --- Tab 3: Check-Ins History & Local CSV/JSON Export ---

    def _build_history_tab(self) -> None:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=16, margin_bottom=24, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(box)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search check-ins and notes...", hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self._refresh_history())
        top_row.append(self.search_entry)

        btn_csv = Gtk.Button(label="Export CSV")
        btn_csv.connect("clicked", self._on_export_file, "csv")
        top_row.append(btn_csv)

        btn_json = Gtk.Button(label="Export JSON")
        btn_json.connect("clicked", self._on_export_file, "json")
        top_row.append(btn_json)
        box.append(top_row)

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for key, lbl in (
            ("all", "All"),
            ("already_relaxed", 'In "N"'),
            ("reset_now", "Caught & Reset"),
            ("urge_resisted", "5s Urge"),
            ("unanswered", "Pending"),
        ):
            b = Gtk.Button(label=lbl)
            b.connect("clicked", self._on_history_filter_clicked, key)
            filter_row.append(b)
        box.append(filter_row)

        self.history_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.history_list_box)

        self.view_stack.add_titled_with_icon(
            scroll, "history", "History", "view-list-symbolic"
        )

    # --- Tab 4: The Declench Method & Jaw Exercises Guide ---

    def _build_method_tab(self) -> None:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=16, margin_bottom=24, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(box)

        cards = [
            (
                "STEP 1 · ANATOMICAL ANCHOR",
                'The Passive "N" Resting Posture',
                'Say the letter "N" silently and notice where the tip of your tongue lands: right on the palate ridge behind your upper front teeth. Keep your lips gently closed and let a 2 to 3 mm gap float between your upper and lower teeth. Your jaw muscles naturally release when parked in passive "N".',
            ),
            (
                "STEP 2 · DEFEATING HABITUATION",
                "Why ±25% Random Daytime Polling Works",
                "Fixed hourly reminders turn into invisible background noise within days. Declench adds ±25% random jitter around your interval so each desktop notification acts as a genuine pattern interrupt while you code, write, or review pull requests.",
            ),
            (
                "STEP 3 · GENTLE JAW UNWIND EXERCISES",
                "Quick Jaw Stretch, Cheek Massage & Temple Release",
                "1. Controlled N-Tongue Opening: Keep your tongue tip on 'N' and slowly open within an easy, pain-free range for one deep breath.\n"
                "2. Masseter Cheek Circles: Use two fingertips to massage the thick jaw muscle below your cheekbones in slow circles for 10 seconds.\n"
                "3. Temple & Shoulder Drop: Lightly massage your temples, exhale slowly, drop your shoulders away from your ears, and settle back into 'N'.",
            ),
            (
                "STEP 4 · EXTINCTION BURSTS",
                "Riding Out the 5-Second Urge",
                "When breaking a bracing habit, your jaw often fires a brief urge to clench back down. Hold passive 'N' for 5 calm seconds and click +1 5-Second Urge Resisted to weaken the old reflex loop.",
            ),
            (
                "HONEST SCIENCE · NON-MEDICAL TOOL",
                "Privacy-First Local Habit Coach",
                "Declench is a personal behavioral habit-awareness tool inspired by physical-therapy posture cueing and Ecological Momentary Assessment. It is not a medical device and does not diagnose or treat TMJ or TMD conditions.",
            ),
        ]
        for badge, title, body_txt in cards:
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            c.add_css_class("declench-card")
            b_lbl = Gtk.Label(label=badge, xalign=0)
            b_lbl.add_css_class("declench-badge")
            t_lbl = Gtk.Label(label=title, xalign=0, wrap=True)
            t_lbl.add_css_class("title-4")
            p_lbl = Gtk.Label(label=body_txt, xalign=0, wrap=True)
            p_lbl.add_css_class("declench-muted")
            c.append(b_lbl)
            c.append(t_lbl)
            c.append(p_lbl)
            box.append(c)

        btn_web_guide = Gtk.Button(label="Read Illustrated Guide on declench.app", halign=Gtk.Align.START)
        btn_web_guide.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(METHOD_WEB_URL, None))
        box.append(btn_web_guide)

        self.view_stack.add_titled_with_icon(
            scroll, "method", "Method", "info-outline-symbolic"
        )

    # --- Tab 5: Mobile & Declench Pro Cloud Sync (Upsell + Optional Login) ---

    def _build_everywhere_tab(self) -> None:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=16, margin_bottom=24, margin_start=16, margin_end=16)
        scroll.set_child(clamp)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(box)

        # Hero Value Card
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.add_css_class("declench-pro-banner")

        badge = Gtk.Label(label="DECLENCH PRO · IPHONE, APPLE WATCH, ANDROID & WEB", xalign=0)
        badge.add_css_class("declench-badge")
        hero.append(badge)

        title = Gtk.Label(
            label="Keep Declenching After You Step Away From Your Linux Desk",
            xalign=0,
            wrap=True,
        )
        title.add_css_class("title-2")
        hero.append(title)

        desc = Gtk.Label(
            label=(
                "Declench on Linux is 100% free forever with zero account required. "
                "When you want continuous habit rewiring while driving, running errands, or away from your keyboard, "
                "Declench Pro connects all your devices into one adaptive schedule and streak."
            ),
            xalign=0,
            wrap=True,
        )
        desc.add_css_class("declench-muted")
        hero.append(desc)

        bullets = Gtk.Label(
            label=(
                "• Native iPhone & Apple Watch app with 1-tap Lock Screen check-ins and Time-Sensitive Focus support\n"
                "• Native Android Material 3 app with instant notification check-in buttons\n"
                "• Automatic two-way cloud sync between this Linux app, your phone, and declench.app\n"
                "• 5-Day Free Trial ($0.00 due today, no credit card required), then $4.99/month or $39.99/year"
            ),
            xalign=0,
            wrap=True,
        )
        hero.append(bullets)

        cta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin_top=6)
        btn_trial = Gtk.Button(label="Start 5-Day Free Trial on declench.app")
        btn_trial.add_css_class("declench-primary-cta")
        btn_trial.connect(
            "clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(SIGNUP_TRIAL_URL, None)
        )
        cta_row.append(btn_trial)

        btn_mobile = Gtk.Button(label="iPhone & Android Apps")
        btn_mobile.connect(
            "clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(HOMEPAGE_MOBILE_URL, None)
        )
        cta_row.append(btn_mobile)
        hero.append(cta_row)
        box.append(hero)

        # Optional Cloud Account Sign-In & Sync Group (Browser OAuth + PKCE Deep Link)
        sync_group = Adw.PreferencesGroup(
            title="Optional Declench Cloud Sync",
            description=(
                "Already started a 5-day trial or Declench Pro membership? Click Sign In with Browser "
                "to connect via Google, Apple, or Email without typing your password here."
            ),
        )
        self.lbl_cloud_status = Adw.ActionRow(
            title="Sync Status",
            subtitle="Local-Only Mode (No network requests are made)",
            use_markup=False,
        )
        sync_group.add(self.lbl_cloud_status)

        fallback_expander = Adw.ExpanderRow(
            title="Browser blocked the app popup? Paste sign-in link",
            subtitle="Use only if a sandboxed browser blocks declench:// links",
            use_markup=False,
        )
        self.entry_callback_uri = Adw.EntryRow(
            title="One-Time Callback Link (declench://...)",
            use_markup=False,
        )
        btn_paste_complete = Gtk.Button(
            label="Complete Sign-In",
            valign=Gtk.Align.CENTER,
        )
        btn_paste_complete.connect("clicked", self._on_manual_callback_clicked)
        self.entry_callback_uri.add_suffix(btn_paste_complete)
        fallback_expander.add_row(self.entry_callback_uri)
        sync_group.add(fallback_expander)

        sync_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin_top=8)
        self.btn_cloud_login = Gtk.Button(label="Sign In with Browser")
        self.btn_cloud_login.add_css_class("declench-primary-cta")
        self.btn_cloud_login.connect("clicked", self._on_cloud_login_clicked)
        sync_btns.append(self.btn_cloud_login)

        self.btn_cloud_sync_now = Gtk.Button(label="Sync Now")
        self.btn_cloud_sync_now.connect("clicked", self._on_cloud_sync_clicked)
        sync_btns.append(self.btn_cloud_sync_now)

        btn_billing = Gtk.Button(label="Manage Plan / Billing")
        btn_billing.connect(
            "clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(BILLING_WEB_URL, None)
        )
        sync_btns.append(btn_billing)

        self.btn_cloud_logout = Gtk.Button(label="Disconnect Cloud")
        self.btn_cloud_logout.connect("clicked", self._on_cloud_logout_clicked)
        sync_btns.append(self.btn_cloud_logout)

        box.append(sync_group)
        box.append(sync_btns)

        self.view_stack.add_titled_with_icon(
            scroll, "everywhere", "Mobile & Sync", "phone-symbolic"
        )

    # --- UI State Refresh ---

    def refresh_all(self) -> None:
        prog, stats = self.store.get_program_and_stats()
        theme_key = self.store.get_setting("theme", "midnight")
        palette = get_theme(theme_key)

        # Coach view updates
        self.lbl_phase.set_text(f"DAY {stats['day_number']} · {stats['rewiring_phase'].upper()}")
        self.lbl_headline.set_text(prog["coach_headline"])
        self.lbl_coach_note.set_text(prog["coach_note"])
        self.goal_bar.set_fraction(max(0.05, min(1.0, prog["goal_progress_pct"] / 100.0)))

        if prog["is_temporarily_paused"]:
            state_txt = f"Silenced temporarily · {prog['schedule_description']}"
        elif not prog["enabled"]:
            state_txt = "Paused indefinitely"
        elif prog["in_quiet_hours"]:
            state_txt = f"Quiet hours active ({prog['active_start_time']} to {prog['active_end_time']}) · Sleep protected"
        else:
            prefix = "Active & Cloud Synced" if self.cloud.is_signed_in else "Active on Linux"
            state_txt = f"{prefix} · {prog['schedule_description']}"
        self.lbl_sched_status.set_text(state_txt)
        self.btn_resume.set_sensitive(prog["is_temporarily_paused"] or not prog["enabled"])

        self.lbl_streak_val.set_text(str(stats["current_streak"]))
        auto_pct = stats["automaticity_rate_pct"]
        self.lbl_auto_val.set_text(f"{int(round(auto_pct))}%" if auto_pct is not None else "-")
        self.lbl_urge_val.set_text(str(stats["urges_resisted_count"]))

        # Chart update
        ts = self.store.get_timeseries(self.selected_granularity)
        self.lbl_chart_window.set_text(ts["window_label"])
        self.chart.set_data(ts, palette)
        peak = ts.get("peak_tension")
        if peak:
            self.lbl_peak_tension.set_text(
                f"Peak clench window: {peak['label']} ({peak['tension_count']} tension catches)"
            )
        else:
            self.lbl_peak_tension.set_text("Check in on desktop nudges to populate your posture graph.")

        # Recent logs
        self._clear_box(self.recent_list_box)
        recent = self.store.list_logs(limit=6)
        self.btn_clear_unanswered.set_sensitive(stats["pending_checkins"] > 0)
        for item in recent:
            self.recent_list_box.append(self._build_log_row_card(item, compact=True))

        # Promo banner visibility
        dismissed = self.store.get_setting("dismissed_mobile_promo", "0") == "1"
        self.promo_banner.set_visible(not dismissed and not self.cloud.is_signed_in)

        # Populate Schedule form controls without triggering recursive writes
        self._updating_ui = True
        try:
            self.row_random_mode.set_active(prog["interval_mode"] == "random")
            self.spin_jitter.set_value(prog["jitter_pct"])
            try:
                iv_mins = interval_to_seconds(prog["interval_value"], prog["interval_unit"]) / 60.0
            except ValueError:
                iv_mins = prog["interval_value"]
            try:
                tgt_mins = (
                    interval_to_seconds(
                        prog["target_interval_value"], prog["target_interval_unit"]
                    )
                    / 60.0
                )
            except ValueError:
                tgt_mins = prog["target_interval_value"]
            self.spin_interval.set_value(max(1.0, min(240.0, iv_mins)))
            self.row_progressive.set_active(prog["progressive_enabled"])
            self.spin_target.set_value(max(1.0, min(360.0, tgt_mins)))
            self.row_exercises.set_active(prog["exercises_enabled"])
            self.entry_start.set_text(prog["active_start_time"])
            self.entry_end.set_text(prog["active_end_time"])
            self.entry_tz.set_text(prog["timezone"])
            self.row_sound.set_active(prog["sound_enabled"])
            self.row_background.set_active(self._background_enabled())

            theme_idx = next((i for i, t in enumerate(THEMES) if t.key == palette.key), 0)
            if self.combo_theme.get_selected() != theme_idx:
                self.combo_theme.set_selected(theme_idx)
        finally:
            self._updating_ui = False

        self._refresh_history()
        self._refresh_cloud_ui(stats=stats)

    def _refresh_history(self) -> None:
        self._clear_box(self.history_list_box)
        q = self.search_entry.get_text() if hasattr(self, "search_entry") else ""
        logs = self.store.list_logs(
            limit=80, outcome=self.history_outcome_filter, search=q
        )
        if not logs:
            empty = Gtk.Label(
                label="No check-ins match this filter yet.",
                margin_top=24,
                margin_bottom=24,
            )
            empty.add_css_class("declench-muted")
            self.history_list_box.append(empty)
            return
        for item in logs:
            self.history_list_box.append(self._build_log_row_card(item, compact=False))

    def _refresh_cloud_ui(self, stats: Optional[dict[str, Any]] = None) -> None:
        user = self.cloud.cached_user
        if self.cloud.is_signed_in and user:
            status = str(user.get("subscription_status") or "active").replace("_", " ").title()
            checkins = int((stats or {}).get("total_checkins") or 0)
            self.lbl_cloud_status.set_subtitle(
                f"Connected as {user.get('email')} ({status} · {checkins} check-ins synced)"
            )
            self.btn_cloud_login.set_label("Switch Account in Browser")
            self.btn_cloud_sync_now.set_sensitive(not self._sync_in_flight)
            self.btn_cloud_logout.set_sensitive(True)
        else:
            self.lbl_cloud_status.set_subtitle(
                "Local-Only Mode (100% offline SQLite, zero network traffic)"
            )
            self.btn_cloud_login.set_label("Sign In with Browser")
            self.btn_cloud_sync_now.set_sensitive(False)
            self.btn_cloud_logout.set_sensitive(False)

    def _build_log_row_card(self, item: dict[str, Any], compact: bool) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("declench-card")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_lbl = Gtk.Label(label=item["title"], xalign=0, hexpand=True, wrap=True)
        title_lbl.add_css_class("heading")
        top.append(title_lbl)

        out_lbl = Gtk.Label(label=item["display_outcome"], xalign=1)
        out_lbl.add_css_class("declench-badge")
        top.append(out_lbl)
        card.append(top)

        ts_short = (item["triggered_at"] or "")[:16].replace("T", " ")
        meta_lbl = Gtk.Label(label=f"{ts_short} UTC", xalign=0)
        meta_lbl.add_css_class("caption")
        meta_lbl.add_css_class("declench-muted")
        card.append(meta_lbl)

        if not compact and item.get("message"):
            msg_lbl = Gtk.Label(label=item["message"], xalign=0, wrap=True)
            msg_lbl.add_css_class("declench-muted")
            card.append(msg_lbl)

        if item["followed"] is None and item["status"] == "sent":
            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for out_key, lbl in (
                ("already_relaxed", 'Already in "N"'),
                ("reset_now", "Caught & Reset"),
                ("urge_resisted", "5s Urge"),
            ):
                b = Gtk.Button(label=lbl)
                b.connect("clicked", self._on_log_checkin_clicked, item["id"], out_key)
                btn_row.append(b)
            card.append(btn_row)

        return card

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    # --- Event Handlers ---

    def _on_quick_urge_clicked(self, _btn: Gtk.Button) -> None:
        self.store.record_quick_urge()
        self.show_toast("5-second urge resisted logged")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self.trigger_background_cloud_sync(silent=True)

    def _on_log_checkin_clicked(self, _btn: Gtk.Button, log_id: int, outcome: str) -> None:
        self.store.record_feedback(log_id, outcome)
        self.get_application().withdraw_active_nudge_banner()
        self.show_toast("Check-in saved")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self.trigger_background_cloud_sync(silent=True)

    def _on_pause(self, mode: str, minutes: int) -> None:
        prog = self.store.pause_program(mode=mode, minutes=minutes)
        self.get_application().withdraw_active_nudge_banner()
        if mode == "until_morning":
            self.show_toast(f"Silenced until {prog['active_start_time']}")
        else:
            self.show_toast(f"Silenced for {minutes} minutes")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self._run_cloud_mutation(lambda: self.cloud.push_pause(self.store, mode, minutes))

    def _on_resume_clicked(self, _btn: Gtk.Button) -> None:
        self.store.resume_program()
        self.show_toast("Posture nudges resumed")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self._run_cloud_mutation(lambda: self.cloud.push_resume(self.store))

    def _on_clear_unanswered(self, _btn: Gtk.Button) -> None:
        res = self.store.clear_unanswered_checkins()
        self.get_application().withdraw_active_nudge_banner()
        self.show_toast(f"Cleared {res['updated_count']} unanswered check-in(s) with zero penalty")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self._run_cloud_mutation(lambda: self.cloud.push_clear_unanswered())

    def _on_granularity_clicked(self, _btn: Gtk.Button, key: str) -> None:
        self.selected_granularity = key
        self.refresh_all()

    def _on_history_filter_clicked(self, _btn: Gtk.Button, key: str) -> None:
        self.history_outcome_filter = key
        self._refresh_history()

    def _on_dismiss_promo(self, _btn: Gtk.Button) -> None:
        self.store.set_setting("dismissed_mobile_promo", "1")
        self.promo_banner.set_visible(False)

    def _on_theme_changed(self, combo: Adw.ComboRow, _pspec: Any) -> None:
        if self._updating_ui:
            return
        idx = int(combo.get_selected())
        if 0 <= idx < len(THEMES):
            chosen = THEMES[idx]
            self.store.set_setting("theme", chosen.key)
            app = self.get_application()
            if app is not None:
                app.apply_theme(chosen.key)
            ts = self.store.get_timeseries(self.selected_granularity)
            self.chart.set_data(ts, chosen)
            if self.cloud.is_signed_in:
                self._run_cloud_mutation(lambda: self.cloud.push_theme(chosen.key))

    def _on_save_schedule(self, _btn: Gtk.Button) -> None:
        try:
            bg_on = self.row_background.get_active()
            self.store.set_setting("run_in_background", "1" if bg_on else "0")
            self.set_hide_on_close(bg_on)

            payload = {
                "interval_mode": "random" if self.row_random_mode.get_active() else "fixed",
                "jitter_pct": int(self.spin_jitter.get_value()),
                "interval_value": float(self.spin_interval.get_value()),
                "interval_unit": "minutes",
                "progressive_enabled": self.row_progressive.get_active(),
                "target_interval_value": float(self.spin_target.get_value()),
                "target_interval_unit": "minutes",
                "adaptive_feedback": self.row_progressive.get_active(),
                "exercises_enabled": self.row_exercises.get_active(),
                "active_start_time": self.entry_start.get_text().strip(),
                "active_end_time": self.entry_end.get_text().strip(),
                "timezone": self.entry_tz.get_text().strip(),
                "sound_enabled": self.row_sound.get_active(),
            }
            self.store.update_program(payload)
            self.show_toast(
                "Schedule saved & syncing with Cloud"
                if self.cloud.is_signed_in
                else "Schedule saved locally"
            )
            self.refresh_all()
            if self.cloud.is_signed_in:
                cloud_payload = {k: v for k, v in payload.items() if k != "sound_enabled"}
                self._run_cloud_mutation(
                    lambda: self.cloud.push_program_settings(self.store, cloud_payload)
                )
        except ValueError as exc:
            self.show_toast(str(exc))

    def _on_reset_interval(self, _btn: Gtk.Button) -> None:
        self.store.reset_interval()
        self.show_toast("Interval reset to starting baseline")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self._run_cloud_mutation(lambda: self.cloud.push_reset_interval(self.store))

    def _on_restore_defaults(self, _btn: Gtk.Button) -> None:
        self.store.restore_defaults()
        self.show_toast("Restored recommended schedule defaults")
        self.refresh_all()
        if self.cloud.is_signed_in:
            self._run_cloud_mutation(lambda: self.cloud.push_restore_defaults(self.store))

    def _on_export_file(self, _btn: Gtk.Button, fmt: str) -> None:
        dialog = Gtk.FileDialog(initial_name=f"declench-checkins.{fmt}")

        def _done(dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.save_finish(res)
                if gfile and gfile.get_path():
                    content = (
                        self.store.export_json() if fmt == "json" else self.store.export_csv()
                    )
                    Path(gfile.get_path()).write_text(content, encoding="utf-8")
                    self.show_toast(f"Exported {fmt.upper()} successfully")
            except GLib.Error:
                pass

        dialog.save(self, None, _done)

    # --- Optional Cloud Sync Handlers (Browser PKCE + Deep-Link Callback) ---

    def _run_cloud_mutation(self, fn: Callable[[], Any]) -> None:
        def _worker() -> None:
            with self._sync_lock:
                try:
                    fn()
                except CloudSyncError:
                    return
            GLib.idle_add(self._apply_synced_state, None, False)

        threading.Thread(target=_worker, daemon=True).start()

    def trigger_background_cloud_sync(
        self, *, silent: bool = True, announce_imports: bool = False
    ) -> None:
        if not self.cloud.is_signed_in:
            return
        if silent and self._sync_in_flight:
            return
        self._sync_in_flight = True
        if not silent:
            self.btn_cloud_sync_now.set_sensitive(False)

        def _worker() -> None:
            toast_msg: Optional[str] = None
            try:
                with self._sync_lock:
                    result = self.cloud.sync_with_local_store(self.store)
                imported = int(result.get("imported_logs") or 0)
                total = int((result.get("stats") or {}).get("total_checkins") or 0)
                if not silent:
                    toast_msg = f"Cloud sync complete ({total} check-in(s) synced)"
                elif announce_imports and imported > 0:
                    toast_msg = f"Loaded {imported} check-in(s) from Declench Cloud"
            except CloudSyncError as exc:
                if not silent:
                    toast_msg = str(exc)
            finally:
                self._sync_in_flight = False
                GLib.idle_add(self._apply_synced_state, toast_msg, False)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cloud_login_clicked(self, _btn: Gtk.Button) -> None:
        auth_url = self.cloud.start_browser_sign_in()
        Gio.AppInfo.launch_default_for_uri(auth_url, None)
        self.lbl_cloud_status.set_subtitle(
            "Waiting for browser approval... Complete sign-in in your browser."
        )
        self.show_toast("Opened declench.app in your browser")

    def _on_manual_callback_clicked(self, _btn: Gtk.Button) -> None:
        raw_uri = self.entry_callback_uri.get_text().strip()
        if not raw_uri:
            self.show_toast("Paste the declench:// callback link first")
            return
        self.handle_auth_callback_uri(raw_uri)

    def handle_auth_callback_uri(self, callback_uri: str) -> None:
        self.btn_cloud_login.set_sensitive(False)
        self.lbl_cloud_status.set_subtitle("Completing secure PKCE exchange...")

        def _worker() -> None:
            self._sync_in_flight = True
            try:
                with self._sync_lock:
                    user = self.cloud.complete_browser_callback(callback_uri)
                    try:
                        result = self.cloud.sync_with_local_store(self.store)
                        total = int((result.get("stats") or {}).get("total_checkins") or 0)
                        msg = f"Connected as {result['user']['email']} ({total} check-in(s) synced)"
                    except CloudSyncError as sync_exc:
                        msg = f"Signed in as {user.get('email')} ({sync_exc})"
                GLib.idle_add(self._apply_synced_state, msg, True)
            except CloudSyncError as exc:
                GLib.idle_add(self._apply_synced_state, str(exc), False)
            finally:
                self._sync_in_flight = False

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cloud_sync_clicked(self, _btn: Gtk.Button) -> None:
        self.trigger_background_cloud_sync(silent=False)

    def _apply_synced_state(self, message: Optional[str], clear_uri: bool) -> bool:
        self.btn_cloud_login.set_sensitive(True)
        if clear_uri:
            self.entry_callback_uri.set_text("")
        app = self.get_application()
        if app is not None:
            app.apply_theme(self.store.get_setting("theme", "midnight"))
            _, stats = self.store.get_program_and_stats()
            if int(stats.get("pending_checkins") or 0) == 0:
                app.withdraw_active_nudge_banner()
        if message:
            self.show_toast(message)
        self.refresh_all()
        return False

    def _on_cloud_logout_clicked(self, _btn: Gtk.Button) -> None:
        self.cloud.sign_out()
        self.show_toast("Disconnected from Declench Cloud (local data preserved)")
        self.refresh_all()

