import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import random
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest

from declench_gnome.cloud_sync import CloudSyncError, DeclenchCloudClient, SIGNUP_TRIAL_URL
from declench_gnome.coaching import LocalCoachingEngine
from declench_gnome.local_store import LocalDeclenchStore
from declench_gnome.schedule_math import (
    apply_active_hours,
    apply_random_jitter,
    is_in_quiet_hours,
    next_awake_morning_utc,
    step_progressive_interval,
)
from declench_gnome.themes import THEMES, build_gtk_css, get_theme, hex_to_rgb_floats


def test_default_program_provisioning(tmp_path: Path) -> None:
    db_file = tmp_path / "declench.sqlite3"
    store = LocalDeclenchStore(db_path=db_file, rng=random.Random(42))

    program, stats = store.get_program_and_stats()
    assert program["id"] == 1
    assert program["interval_mode"] == "random"
    assert program["jitter_pct"] == 25
    assert program["interval_value"] == 20.0
    assert program["target_interval_value"] == 90.0
    assert program["progressive_enabled"] is True
    assert program["adaptive_feedback"] is True
    assert program["exercises_enabled"] is True
    assert program["enabled"] is True
    assert program["status_state"] == "active"

    assert stats["total_checkins"] == 0
    assert stats["pending_checkins"] == 0
    assert stats["current_streak"] == 0
    assert stats["follow_rate_pct"] is None


def test_random_jitter_bounds_and_progressive_stepping() -> None:
    rng = random.Random(1234)
    samples = [apply_random_jitter(1200.0, jitter_pct=25, rng=rng) for _ in range(100)]
    assert all(900.0 <= s <= 1500.0 for s in samples)
    assert len(set(samples)) > 50

    widened = step_progressive_interval(
        current_seconds=1200.0,
        target_seconds=5400.0,
        step_type="linear",
        step_value=5.0,
        step_unit="minutes",
    )
    assert widened == 1500.0

    capped = step_progressive_interval(
        current_seconds=5300.0,
        target_seconds=5400.0,
        step_type="linear",
        step_value=5.0,
        step_unit="minutes",
    )
    assert capped == 5400.0


def test_quiet_hours_snapping_and_until_morning_pause(tmp_path: Path) -> None:
    # 23:15 America/New_York (03:15 UTC next day during EDT) -> snaps to 08:30 next morning
    late_utc = datetime(2026, 7, 15, 3, 15, tzinfo=timezone.utc)
    assert is_in_quiet_hours(late_utc, "America/New_York", "08:30", "21:30") is True

    snapped = apply_active_hours(late_utc, "America/New_York", "08:30", "21:30")
    assert snapped == datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)

    # Early morning 01:00 EDT (05:00 UTC) wakes at 08:30 EDT same calendar day
    early_utc = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)
    wake = next_awake_morning_utc(early_utc, "America/New_York", "08:30")
    assert wake == datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)

    store = LocalDeclenchStore(db_path=tmp_path / "pause.sqlite3", rng=random.Random(7))
    store.update_program(
        {
            "timezone": "America/New_York",
            "active_start_time": "08:30",
            "active_end_time": "21:30",
        },
        now=datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
    )

    paused = store.pause_program(
        mode="until_morning",
        now=datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc),
    )
    assert paused["status_state"] == "paused_temp"
    assert paused["is_temporarily_paused"] is True
    assert paused["notifications_active"] is False

    # During quiet/paused window due_tick should not fire
    assert store.due_tick(now=datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)) is None

    # Once paused_until passes during daytime, pause auto-expires
    prog_after, _ = store.get_program_and_stats(
        now=datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    )
    assert prog_after["status_state"] == "active"
    assert prog_after["is_temporarily_paused"] is False


def test_single_slot_supersede_and_penalty_free_clear(tmp_path: Path) -> None:
    store = LocalDeclenchStore(db_path=tmp_path / "supersede.sqlite3", rng=random.Random(99))
    t0 = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)

    # First check-in answered positively to establish a streak and 100% follow rate
    first = store.trigger_now(now=t0)
    _, _, stats_1 = store.record_feedback(first["id"], "already_relaxed", now=t0 + timedelta(seconds=10))
    assert stats_1["current_streak"] == 1
    assert stats_1["follow_rate_pct"] == 100.0

    # Fire two unanswered nudges in a row -> older unanswered should auto-supersede
    nudge_a = store.trigger_now(now=t0 + timedelta(minutes=20))
    nudge_b = store.trigger_now(now=t0 + timedelta(minutes=40))
    assert nudge_b["id"] != nudge_a["id"]

    _, stats_mid = store.get_program_and_stats(now=t0 + timedelta(minutes=41))
    assert stats_mid["pending_checkins"] == 1
    assert stats_mid["current_streak"] == 1
    assert stats_mid["follow_rate_pct"] == 100.0

    # Clear remaining unanswered check-in with zero penalty
    cleared = store.clear_unanswered_checkins(now=t0 + timedelta(minutes=42))
    assert cleared["cleared"] is True
    assert cleared["updated_count"] == 1
    assert cleared["stats"]["pending_checkins"] == 0
    assert cleared["stats"]["current_streak"] == 1
    assert cleared["stats"]["follow_rate_pct"] == 100.0
    assert store.latest_pending_log_id() is None

    # Superseded items are hidden from default history feed
    visible_logs = store.list_logs()
    assert len(visible_logs) == 1
    assert visible_logs[0]["id"] == first["id"]


def test_progressive_widening_and_adaptive_tightening(tmp_path: Path) -> None:
    store = LocalDeclenchStore(db_path=tmp_path / "adaptive.sqlite3", rng=random.Random(11))
    t0 = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)

    n1 = store.trigger_now(now=t0)
    _, prog_up, stats_up = store.record_feedback(n1["id"], "already_relaxed", now=t0 + timedelta(seconds=5))
    assert prog_up["current_interval_seconds"] == 1500.0  # 20m -> 25m
    assert stats_up["already_relaxed_count"] == 1

    n2 = store.trigger_now(now=t0 + timedelta(minutes=25))
    _, prog_tight, stats_tight = store.record_feedback(n2["id"], "reset_now", now=t0 + timedelta(minutes=25, seconds=5))
    assert prog_tight["current_interval_seconds"] < 1500.0  # adaptive 0.95 tightening
    assert stats_tight["reset_now_count"] == 1
    assert stats_tight["current_streak"] == 2  # Caught & Reset still counts as followed!

    urge_log, stats_urge = store.record_quick_urge(note="Code review tension", now=t0 + timedelta(minutes=30))
    assert urge_log["outcome"] == "urge_resisted"
    assert stats_urge["urges_resisted_count"] == 1
    assert stats_urge["current_streak"] == 3


def test_jaw_exercise_prompts_and_no_em_dashes(tmp_path: Path) -> None:
    store = LocalDeclenchStore(db_path=tmp_path / "exercise.sqlite3", rng=random.Random(5))
    ex_log = store.trigger_now(
        force_exercise=True,
        now=datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc),
    )
    assert any(
        word in ex_log["title"]
        for word in ("Stretch", "Massage", "Temple", "Unwind")
    )

    # Verify zero em-dashes or en-dashes across generated coaching prompts
    for run_idx in range(1, 15):
        for force_ex in (False, True):
            prompt = LocalCoachingEngine.generate_prompt(
                stats={"total_checkins": run_idx, "current_streak": 4, "automaticity_rate_pct": 75.0},
                last_outcome="reset_now" if run_idx % 3 == 0 else "already_relaxed",
                current_interval_seconds=1800.0,
                start_interval_seconds=1200.0,
                target_interval_seconds=5400.0,
                run_count=run_idx,
                force_exercise=force_ex,
                rng=random.Random(run_idx),
            )
            for text in (
                prompt.title,
                prompt.message,
                prompt.stage_badge,
                prompt.coach_headline,
                prompt.coach_note,
                prompt.goal_milestone,
            ):
                assert "\u2014" not in text
                assert "\u2013" not in text


def test_timeseries_and_exports(tmp_path: Path) -> None:
    store = LocalDeclenchStore(db_path=tmp_path / "analytics.sqlite3", rng=random.Random(21))
    base_t = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)
    store.update_program({"timezone": "America/New_York"}, now=base_t)

    l1 = store.trigger_now(now=base_t)
    store.record_feedback(l1["id"], "already_relaxed", now=base_t + timedelta(seconds=10))

    l2 = store.trigger_now(now=base_t + timedelta(minutes=15))
    store.record_feedback(l2["id"], "reset_now", note="Afternoon sprint", now=base_t + timedelta(minutes=15, seconds=8))

    store.record_quick_urge(note=" resisted bite", now=base_t + timedelta(minutes=25))

    hourly = store.get_timeseries("hourly", now=base_t + timedelta(minutes=30))
    assert hourly["granularity"] == "hourly"
    assert len(hourly["buckets"]) == 24
    assert hourly["max_bucket_total"] >= 3
    assert hourly["peak_tension"] is not None
    assert hourly["peak_tension"]["tension_count"] == 2

    daily = store.get_timeseries("daily", now=base_t + timedelta(minutes=30))
    assert len(daily["buckets"]) == 14

    monthly = store.get_timeseries("monthly", now=base_t + timedelta(minutes=30))
    assert len(monthly["buckets"]) == 12

    csv_data = store.export_csv()
    rows = list(csv.reader(io.StringIO(csv_data)))
    assert rows[0][0] == "id"
    assert len(rows) == 4  # header + 3 recorded logs

    json_payload = json.loads(store.export_json())
    assert "program" in json_payload
    assert "stats" in json_payload
    assert len(json_payload["logs"]) == 3


def test_all_eight_themes_and_css_builder() -> None:
    expected_keys = {
        "midnight",
        "daylight",
        "girl_power",
        "berry_glow",
        "lavender",
        "matcha",
        "sunset",
        "obsidian",
    }
    assert {t.key for t in THEMES} == expected_keys
    assert get_theme("unknown_key").key == "midnight"

    for palette in THEMES:
        css = build_gtk_css(palette)
        assert palette.bg_primary in css
        assert palette.accent_primary in css
        r, g, b = hex_to_rgb_floats(palette.accent_primary)
        assert 0.0 <= r <= 1.0 and 0.0 <= g <= 1.0 and 0.0 <= b <= 1.0


def test_legacy_sqlite_schema_migrates_cloud_log_id(tmp_path: Path) -> None:
    import sqlite3

    db_file = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(str(db_file)) as conn:
        conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE local_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_uuid TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                trigger_source TEXT NOT NULL DEFAULT 'scheduled',
                status TEXT NOT NULL DEFAULT 'sent',
                followed INTEGER,
                outcome TEXT,
                note TEXT,
                feedback_at TEXT,
                interval_seconds_at_trigger REAL,
                synced_to_cloud INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    store = LocalDeclenchStore(db_path=db_file, rng=random.Random(1))
    log, _ = store.record_quick_urge(note="Migrated cleanly")
    assert log["cloud_log_id"] is None


def test_optional_cloud_sync_client_mocked(tmp_path: Path) -> None:
    from urllib.parse import parse_qs, urlparse

    assert "declench.app/signup" in SIGNUP_TRIAL_URL
    assert "utm_source=gnome_linux" in SIGNUP_TRIAL_URL

    store = LocalDeclenchStore(db_path=tmp_path / "sync.sqlite3", rng=random.Random(3))
    store.record_quick_urge(note="Local Linux urge before sync")

    session_file = tmp_path / "cloud_session.json"
    client = DeclenchCloudClient(base_url="https://declench.app", session_file=session_file)
    assert client.is_signed_in is False

    # Start PKCE browser sign-in flow
    auth_url = client.start_browser_sign_in()
    parsed_auth = urlparse(auth_url)
    assert parsed_auth.path == "/auth/desktop/authorize"
    auth_qs = parse_qs(parsed_auth.query)
    expected_state = auth_qs["state"][0]
    expected_challenge = auth_qs["code_challenge"][0]
    assert auth_qs["platform"][0] == "gnome_linux"
    assert client.pending_auth_file.is_file()

    # Mismatched state in callback URI is rejected before hitting network
    with pytest.raises(CloudSyncError, match="state mismatch"):
        client.complete_browser_callback(
            "declench://auth-callback?code=sample-code&state=wrong-state-token"
        )

    now_utc = datetime.now(timezone.utc)
    created_25d_ago = (now_utc - timedelta(days=25)).isoformat()
    server_checkins: List[Dict[str, Any]] = [
        {
            "id": 101,
            "triggered_at_utc": (now_utc - timedelta(hours=4)).isoformat(),
            "outcome": "already_relaxed",
            "followed": True,
            "feedback_at_utc": (now_utc - timedelta(hours=4) + timedelta(seconds=12)).isoformat(),
            "note": "Morning coding session",
            "cadence_seconds": 2100.0,
            "trigger_source": "scheduled",
            "delivery_status": "sent",
            "title": "Effortless 'N' Check",
            "message": "Lips together, teeth apart.",
        },
        {
            "id": 102,
            "triggered_at_utc": (now_utc - timedelta(hours=2)).isoformat(),
            "outcome": "reset_now",
            "followed": True,
            "feedback_at_utc": (now_utc - timedelta(hours=2) + timedelta(seconds=8)).isoformat(),
            "note": "Caught clenching during PR review",
            "cadence_seconds": 2400.0,
            "trigger_source": "scheduled",
            "delivery_status": "sent",
            "title": "Focus Scan: Check Your Jaw",
            "message": "Separate your teeth 2 to 3 mm.",
        },
        {
            "id": 103,
            "triggered_at_utc": (now_utc - timedelta(minutes=15)).isoformat(),
            "outcome": "unanswered",
            "followed": None,
            "feedback_at_utc": None,
            "note": "",
            "cadence_seconds": 2700.0,
            "trigger_source": "scheduled",
            "delivery_status": "sent",
            "title": "Mindful 'N' Posture Check",
            "message": "Check your bite right now.",
        },
    ]

    calls: List[Tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        calls.append((method, path, body))
        if path == "/api/v1/auth/desktop/exchange":
            assert body is not None
            assert body["code"] == "one-time-pkce-code"
            assert client.compute_pkce_challenge(body["code_verifier"]) == expected_challenge
            return {
                "session_token": "jwt-linux-test-token",
                "user": {
                    "id": 7,
                    "email": "linux@example.com",
                    "theme": "matcha",
                    "has_active_access": True,
                    "subscription_status": "active",
                    "created_at": created_25d_ago,
                },
            }
        if path == "/api/v1/auth/me":
            return {
                "authenticated": True,
                "user": {
                    "id": 7,
                    "email": "linux@example.com",
                    "theme": "matcha",
                    "has_active_access": True,
                    "subscription_status": "active",
                    "created_at": created_25d_ago,
                },
            }
        if path == "/api/v1/program" and method == "GET":
            return {
                "program": {
                    "interval_mode": "random",
                    "jitter_pct": 30,
                    "interval_value": 25.0,
                    "interval_unit": "minutes",
                    "progressive_enabled": True,
                    "target_interval_value": 120.0,
                    "target_interval_unit": "minutes",
                    "current_interval_seconds": 2700.0,
                    "adaptive_feedback": True,
                    "exercises_enabled": True,
                    "exercise_every_n": 6,
                    "active_start_time": "09:00",
                    "active_end_time": "22:00",
                    "timezone": "America/New_York",
                    "enabled": True,
                    "run_count": 18,
                    "created_at": created_25d_ago,
                },
                "stats": {"total_checkins": 3, "current_streak": 3},
            }
        if path == "/api/v1/urges" and method == "POST":
            new_urge = {
                "id": 104,
                "triggered_at_utc": now_utc.isoformat(),
                "outcome": "urge_resisted",
                "followed": True,
                "feedback_at_utc": now_utc.isoformat(),
                "note": (body or {}).get("note") or "",
                "cadence_seconds": None,
                "trigger_source": "self_report",
                "delivery_status": "recorded",
                "title": "5s Urge Resisted (Self-Check)",
                "message": "Self-logged 5-second clench urge resisted.",
            }
            server_checkins.insert(0, new_urge)
            return {"log": {"id": 104}, "stats": {"total_checkins": 3}}
        if path == "/api/v1/logs/export?format=json":
            return {"count": len(server_checkins), "checkins": list(server_checkins)}
        if path == "/api/v1/logs/103/feedback" and method == "POST":
            assert body is not None
            assert body["outcome"] == "already_relaxed"
            for row in server_checkins:
                if row["id"] == 103:
                    row["followed"] = True
                    row["outcome"] = "already_relaxed"
                    row["feedback_at_utc"] = now_utc.isoformat()
            return {"ok": True}
        if path == "/api/v1/account/theme" and method == "PUT":
            assert body == {"theme": "obsidian"}
            return {"theme": "obsidian", "user": {"id": 7, "email": "linux@example.com", "theme": "obsidian"}}
        if path == "/api/v1/devices" and method == "POST":
            assert body is not None
            dev = {
                "id": 501,
                "platform": body.get("platform", "linux"),
                "device_name": body.get("device_name", "Linux Desktop"),
                "fcm_token": body.get("fcm_token"),
                "enabled": bool(body.get("enabled", True)),
            }
            return {"device": dev, "devices": [dev], "active_device_count": 1 if dev["enabled"] else 0}
        if path == "/api/v1/devices" and method == "GET":
            return {"devices": [], "active_device_count": 0}
        raise AssertionError(f"Unexpected request {method} {path}")

    with patch.object(client, "_request_json", side_effect=fake_request):
        user = client.complete_browser_callback(
            f"declench://auth-callback?code=one-time-pkce-code&state={expected_state}"
        )
        assert user["email"] == "linux@example.com"
        assert client.is_signed_in is True
        assert session_file.is_file()
        assert not client.pending_auth_file.exists()

        sync_res = client.sync_with_local_store(store)
        assert sync_res["pushed_urges"] == 1
        assert sync_res["imported_logs"] == 3  # 101, 102, 103 imported; 104 deduplicated in place!

        # Verify theme, progression, Day #, stats, chart, and history all match server!
        assert store.get_setting("theme") == "matcha"
        prog, stats = store.get_program_and_stats()
        assert prog["interval_value"] == 25.0
        assert prog["target_interval_value"] == 120.0
        assert prog["current_interval_seconds"] == 2700.0  # Preserved 45m server progression!
        assert prog["jitter_pct"] == 30
        assert prog["run_count"] == 18
        assert stats["day_number"] >= 25
        assert stats["total_checkins"] == 3  # 101 + 102 + 104 answered
        assert stats["pending_checkins"] == 1  # 103 pending
        assert stats["current_streak"] == 3
        assert stats["urges_resisted_count"] == 1
        assert len(store.list_logs(limit=50)) == 4

        ts = store.get_timeseries("daily")
        assert ts["max_bucket_total"] >= 3

        # Answer pending cloud check-in #103 locally on GNOME -> next sync pushes feedback!
        pending_local = [r for r in store.list_logs() if r["cloud_log_id"] == 103][0]
        store.record_feedback(pending_local["id"], "already_relaxed")

        sync_res_2 = client.sync_with_local_store(store)
        assert sync_res_2["pushed_urges"] == 0
        assert sync_res_2["pushed_feedback"] == 1
        assert sync_res_2["imported_logs"] == 0
        _, stats_after = store.get_program_and_stats()
        assert stats_after["pending_checkins"] == 0
        assert stats_after["total_checkins"] == 4
        assert stats_after["current_streak"] == 4
        assert len(store.list_logs(limit=50)) == 4  # Zero duplicates!

        # Pushing theme from GNOME updates cached cloud user
        client.push_theme("obsidian")
        assert client.cached_user is not None
        assert client.cached_user["theme"] == "obsidian"

    client.sign_out()
    assert client.is_signed_in is False
    assert not session_file.exists()


def test_expired_cloud_trial_keeps_local_linux_free(tmp_path: Path) -> None:
    store = LocalDeclenchStore(db_path=tmp_path / "free_fallback.sqlite3")
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps({"session_token": "tok", "user": {"email": "u@example.com"}}),
        encoding="utf-8",
    )
    client = DeclenchCloudClient(base_url="https://declench.app", session_file=session_file)

    with patch.object(
        client,
        "_request_json",
        return_value={
            "authenticated": True,
            "user": {"email": "u@example.com", "has_active_access": False},
        },
    ):
        with pytest.raises(CloudSyncError, match="Local Linux mode remains 100% free"):
            client.sync_with_local_store(store)


def test_gtk4_libadwaita_window_instantiation(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess
    import time

    gtk_py = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    broadwayd = shutil.which("gtk4-broadwayd")
    if not gtk_py.is_file() or not broadwayd:
        pytest.skip("linux/.venv with PyGObject or gtk4-broadwayd not installed")

    rt_dir = tmp_path / "gtk-rt"
    rt_dir.mkdir(mode=0o700)
    env = os.environ.copy()
    env.update(
        {
            "XDG_RUNTIME_DIR": str(rt_dir),
            "GSETTINGS_BACKEND": "memory",
            "GDK_BACKEND": "broadway",
            "BROADWAY_DISPLAY": ":29",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )

    daemon = subprocess.Popen(
        [broadwayd, ":29"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.25)
    try:
        script = "\n".join(
            [
                "import gi",
                "gi.require_version('Gtk', '4.0')",
                "gi.require_version('Adw', '1')",
                "from gi.repository import Gio, GLib",
                "from declench_gnome.app import DeclenchApplication",
                "from declench_gnome.local_store import LocalDeclenchStore",
                "from declench_gnome.cloud_sync import DeclenchCloudClient",
                f"store = LocalDeclenchStore(db_path={str(tmp_path / 'ui.sqlite3')!r})",
                f"cloud = DeclenchCloudClient(session_file={str(tmp_path / 'sess.json')!r})",
                "app = DeclenchApplication(store=store, cloud=cloud)",
                "app.set_flags(Gio.ApplicationFlags.NON_UNIQUE | Gio.ApplicationFlags.HANDLES_OPEN)",
                "def _check_tabs():",
                "    assert app.window is not None",
                "    assert app.window.view_stack.get_child_by_name('themes') is not None",
                "    assert app.window.view_stack.get_child_by_name('method') is not None",
                "    assert len(app.window.method_pictures) == 4",
                "    app.window.view_stack.set_visible_child_name('method')",
                "    GLib.timeout_add(80, _check_method_pics)",
                "    return False",
                "def _check_method_pics():",
                "    for p in app.window.method_pictures:",
                "        assert p.get_height() >= 380, f'Expected inline diagram height >= 380, got {p.get_height()}'",
                "    app.window._on_theme_card_clicked(None, 'matcha')",
                "    assert store.get_setting('theme') == 'matcha'",
                "    app._on_quit()",
                "    return False",
                "GLib.timeout_add(120, _check_tabs)",
                "raise SystemExit(app.run(['app.declench.linux', 'declench://auth-callback?error=canceled']))",
            ]
        )
        res = subprocess.run([str(gtk_py), "-c", script], env=env, capture_output=True, text=True, timeout=10)
        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        assert "Failed to set text" not in res.stderr
    finally:
        daemon.terminate()
        daemon.wait(timeout=3)


def test_end_to_end_sync_against_real_flask_server(tmp_path: Path) -> None:
    pytest.importorskip("app", reason="Requires full monorepo Flask server package")
    import html
    import re
    from urllib.parse import urlparse
    from app import create_app
    from tests.test_reminders import make_authenticated_trial_client, make_test_conf

    conf = make_test_conf(dry_run=True)
    flask_app = create_app(conf=conf)
    web_client, _user = make_authenticated_trial_client(
        flask_app, email="live-sync@example.com"
    )
    api_client = flask_app.test_client()

    # Populate real server data: custom theme, custom schedule, device, nudge + check-in + urge
    assert web_client.put("/api/v1/account/theme", json={"theme": "lavender"}).status_code == 200
    assert (
        web_client.put(
            "/api/v1/program",
            json={
                "interval_mode": "random",
                "jitter_pct": 35,
                "interval_value": 15,
                "interval_unit": "minutes",
                "progressive_enabled": True,
                "target_interval_value": 90,
                "target_interval_unit": "minutes",
                "active_start_time": "08:00",
                "active_end_time": "21:00",
                "timezone": "America/New_York",
            },
        ).status_code
        == 200
    )
    assert (
        web_client.post(
            "/api/v1/devices",
            json={"platform": "ios", "device_name": "iPhone", "fcm_token": "tok-ios-123456789012345"},
        ).status_code
        == 201
    )
    trig_1 = web_client.post("/api/v1/program/trigger", json={}).get_json()
    log_1_id = trig_1["log"]["id"]
    assert (
        web_client.post(
            f"/api/v1/logs/{log_1_id}/feedback",
            json={"outcome": "already_relaxed", "note": "Relaxed on iPhone"},
        ).status_code
        == 200
    )
    assert (
        web_client.post("/api/v1/urges", json={"note": "Resisted urge in traffic"}).status_code
        == 201
    )
    trig_2 = web_client.post("/api/v1/program/trigger", json={}).get_json()
    pending_server_log_id = trig_2["log"]["id"]

    # Now connect GNOME Linux client via real PKCE browser flow
    store = LocalDeclenchStore(db_path=tmp_path / "real_e2e.sqlite3")
    cloud = DeclenchCloudClient(
        base_url="https://declench.app", session_file=tmp_path / "real_session.json"
    )
    auth_url = cloud.start_browser_sign_in()
    parsed = urlparse(auth_url)
    r_prompt = web_client.get(f"{parsed.path}?{parsed.query}")
    m_tok = re.search(r'name="request_token" value="([^"]+)"', r_prompt.get_data(as_text=True))
    assert m_tok is not None
    r_approve = web_client.post("/auth/desktop/approve", data={"request_token": m_tok.group(1)})
    m_cb = re.search(r'href="(declench://auth-callback\?[^"]+)"', r_approve.get_data(as_text=True))
    assert m_cb is not None
    cb_uri = html.unescape(m_cb.group(1))

    def flask_bridge(
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        if authenticated and cloud.session_token:
            headers["Authorization"] = f"Bearer {cloud.session_token}"
        resp = api_client.open(path, method=method, json=body, headers=headers)
        data = json.loads(resp.get_data(as_text=True))
        if resp.status_code >= 400:
            raise CloudSyncError(data.get("error") or f"HTTP {resp.status_code}")
        return data

    with patch.object(cloud, "_request_json", side_effect=flask_bridge):
        cloud.complete_browser_callback(cb_uri)
        res = cloud.sync_with_local_store(store)
        assert res["imported_logs"] == 3

        assert store.get_setting("theme") == "lavender"
        prog, stats = store.get_program_and_stats()
        assert prog["interval_value"] == 15.0
        assert prog["jitter_pct"] == 35
        assert prog["current_interval_seconds"] > 900.0  # Widened after already_relaxed on iPhone!
        assert stats["total_checkins"] == 2
        assert stats["pending_checkins"] == 1
        assert stats["current_streak"] == 2
        assert stats["urges_resisted_count"] == 1

        # Answer the pending iPhone check-in inside GNOME Linux and sync back to Flask server!
        local_pending = [
            r for r in store.list_logs() if r["cloud_log_id"] == pending_server_log_id
        ][0]
        store.record_feedback(local_pending["id"], "reset_now", note="Answered on Linux")
        res_2 = cloud.sync_with_local_store(store)
        assert res_2["pushed_feedback"] == 1

        # Verify multi-device notification routing between Linux PC, Web, and iPhone
        assert cloud.is_this_device_registered is True
        assert cloud.is_this_device_enabled is True
        assert len(cloud.cached_devices) == 3
        iphone_dev = [d for d in cloud.cached_devices if d["platform"] == "ios"][0]
        linux_dev = cloud.this_device
        assert linux_dev is not None
        assert linux_dev["platform"] == "linux"

        # 1. Route exclusively to this Linux PC -> iPhone and Web are muted automatically
        cloud.register_this_device(enabled=True, only_this=True)
        assert cloud.is_this_device_enabled is True
        assert cloud.active_device_count == 1
        iphone_after_linux_only = [
            d for d in cloud.cached_devices if d["id"] == iphone_dev["id"]
        ][0]
        assert iphone_after_linux_only["enabled"] is False

        # 2. Switch active notifications to iPhone before stepping away -> Linux PC is muted
        cloud.set_only_active_device(iphone_dev["id"])
        assert cloud.is_this_device_enabled is False
        assert cloud.active_device_count == 1
        iphone_after_switch = [
            d for d in cloud.cached_devices if d["id"] == iphone_dev["id"]
        ][0]
        assert iphone_after_switch["enabled"] is True

        # 3. Re-enable this Linux PC -> both Linux and iPhone active
        cloud.set_device_active(linux_dev["id"], True)
        assert cloud.is_this_device_enabled is True
        assert cloud.active_device_count == 2

    # Verify the Flask server now shows all 3 check-ins answered!
    server_stats = web_client.get("/api/v1/stats").get_json()["stats"]
    assert server_stats["total_checkins"] == 3
    assert server_stats["pending_checkins"] == 0
    assert server_stats["reset_now_count"] == 1



