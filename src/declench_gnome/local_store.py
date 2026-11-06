import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import random
import sqlite3
from typing import Any, Dict, List, Optional
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from declench_gnome.coaching import DEFAULT_EXERCISE_EVERY_N, LocalCoachingEngine
from declench_gnome.schedule_math import (
    DEFAULT_JITTER_PCT,
    MIN_INTERVAL_SECONDS,
    compute_next_interval_run,
    detect_system_timezone,
    ensure_utc,
    format_duration_seconds,
    interval_to_seconds,
    is_in_quiet_hours,
    next_awake_morning_utc,
    step_progressive_interval,
    validate_hhmm,
    validate_timezone,
)
from declench_gnome.themes import THEMES

VALID_THEME_KEYS = {t.key for t in THEMES}


DEFAULT_PROGRAM = {
    "interval_mode": "random",
    "jitter_pct": DEFAULT_JITTER_PCT,
    "interval_value": 20.0,
    "interval_unit": "minutes",
    "progressive_enabled": True,
    "target_interval_value": 90.0,
    "target_interval_unit": "minutes",
    "progressive_step_type": "linear",
    "progressive_step_value": 5.0,
    "progressive_step_unit": "minutes",
    "adaptive_feedback": True,
    "exercises_enabled": True,
    "exercise_every_n": DEFAULT_EXERCISE_EVERY_N,
    "active_start_time": "08:30",
    "active_end_time": "21:30",
    "sound_enabled": True,
    "enabled": True,
}


def default_db_path() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
    folder = base / "declench"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "declench.sqlite3"


class LocalDeclenchStore:
    """Zero-server local SQLite store powering Free Local Mode on GNOME Linux."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._rng = rng
        self._init_schema()
        self._ensure_program_row()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_program (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    interval_mode TEXT NOT NULL DEFAULT 'random',
                    jitter_pct INTEGER NOT NULL DEFAULT 25,
                    interval_value REAL NOT NULL DEFAULT 20.0,
                    interval_unit TEXT NOT NULL DEFAULT 'minutes',
                    progressive_enabled INTEGER NOT NULL DEFAULT 1,
                    target_interval_value REAL NOT NULL DEFAULT 90.0,
                    target_interval_unit TEXT NOT NULL DEFAULT 'minutes',
                    progressive_step_type TEXT NOT NULL DEFAULT 'linear',
                    progressive_step_value REAL NOT NULL DEFAULT 5.0,
                    progressive_step_unit TEXT NOT NULL DEFAULT 'minutes',
                    current_interval_seconds REAL NOT NULL DEFAULT 1200.0,
                    adaptive_feedback INTEGER NOT NULL DEFAULT 1,
                    exercises_enabled INTEGER NOT NULL DEFAULT 1,
                    exercise_every_n INTEGER NOT NULL DEFAULT 5,
                    active_start_time TEXT NOT NULL DEFAULT '08:30',
                    active_end_time TEXT NOT NULL DEFAULT '21:30',
                    timezone TEXT NOT NULL,
                    sound_enabled INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    paused_until TEXT,
                    pause_mode TEXT,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_uuid TEXT NOT NULL UNIQUE,
                    cloud_log_id INTEGER,
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

                CREATE INDEX IF NOT EXISTS ix_local_logs_triggered
                    ON local_logs(triggered_at DESC);
                """
            )
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(local_logs)").fetchall()
            }
            if "cloud_log_id" not in cols:
                conn.execute("ALTER TABLE local_logs ADD COLUMN cloud_log_id INTEGER")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ix_local_logs_cloud_id
                    ON local_logs(cloud_log_id)
                    WHERE cloud_log_id IS NOT NULL
                """
            )

    def _ensure_program_row(self, now: Optional[datetime] = None) -> None:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM local_program WHERE id = 1").fetchone()
            if row is not None:
                return
            tz_name = detect_system_timezone()
            start_secs = interval_to_seconds(
                DEFAULT_PROGRAM["interval_value"], DEFAULT_PROGRAM["interval_unit"]
            )
            target_secs = interval_to_seconds(
                DEFAULT_PROGRAM["target_interval_value"], DEFAULT_PROGRAM["target_interval_unit"]
            )
            prompt = LocalCoachingEngine.generate_prompt(
                stats=None,
                current_interval_seconds=start_secs,
                start_interval_seconds=start_secs,
                target_interval_seconds=target_secs,
                rng=self._rng,
            )
            next_run = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=start_secs,
                interval_mode=DEFAULT_PROGRAM["interval_mode"],
                jitter_pct=DEFAULT_PROGRAM["jitter_pct"],
                timezone_name=tz_name,
                active_start_time=DEFAULT_PROGRAM["active_start_time"],
                active_end_time=DEFAULT_PROGRAM["active_end_time"],
                rng=self._rng,
            )
            iso_now = now_utc.isoformat()
            conn.execute(
                """
                INSERT INTO local_program (
                    id, title, message, interval_mode, jitter_pct,
                    interval_value, interval_unit, progressive_enabled,
                    target_interval_value, target_interval_unit,
                    progressive_step_type, progressive_step_value, progressive_step_unit,
                    current_interval_seconds, adaptive_feedback,
                    exercises_enabled, exercise_every_n,
                    active_start_time, active_end_time, timezone,
                    sound_enabled, enabled, next_run_at, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    prompt.title,
                    prompt.message,
                    DEFAULT_PROGRAM["interval_mode"],
                    DEFAULT_PROGRAM["jitter_pct"],
                    DEFAULT_PROGRAM["interval_value"],
                    DEFAULT_PROGRAM["interval_unit"],
                    int(DEFAULT_PROGRAM["progressive_enabled"]),
                    DEFAULT_PROGRAM["target_interval_value"],
                    DEFAULT_PROGRAM["target_interval_unit"],
                    DEFAULT_PROGRAM["progressive_step_type"],
                    DEFAULT_PROGRAM["progressive_step_value"],
                    DEFAULT_PROGRAM["progressive_step_unit"],
                    start_secs,
                    int(DEFAULT_PROGRAM["adaptive_feedback"]),
                    int(DEFAULT_PROGRAM["exercises_enabled"]),
                    int(DEFAULT_PROGRAM["exercise_every_n"]),
                    DEFAULT_PROGRAM["active_start_time"],
                    DEFAULT_PROGRAM["active_end_time"],
                    tz_name,
                    int(DEFAULT_PROGRAM["sound_enabled"]),
                    next_run.isoformat(),
                    iso_now,
                    iso_now,
                ),
            )

    # --- Settings Key/Value Helpers ---

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    # --- Internal Serialization & Stats ---

    @staticmethod
    def _parse_iso(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(val))
        except ValueError:
            return None

    def _compute_stats_in_conn(
        self, conn: sqlite3.Connection, now_utc: datetime
    ) -> tuple[Dict[str, Any], Optional[str]]:
        rows = conn.execute(
            "SELECT * FROM local_logs ORDER BY triggered_at DESC, id DESC"
        ).fetchall()

        total_sent = sum(1 for r in rows if r["status"] in ("sent", "superseded"))
        answered = [r for r in rows if r["followed"] is not None]
        last_outcome = answered[0]["outcome"] if answered else None
        total_checkins = len(answered)
        followed_count = sum(1 for r in answered if int(r["followed"]) == 1)
        missed_count = sum(1 for r in answered if int(r["followed"]) == 0)
        already_relaxed = sum(1 for r in answered if r["outcome"] == "already_relaxed")
        reset_now = sum(1 for r in answered if r["outcome"] == "reset_now")
        urges_resisted = sum(1 for r in answered if r["outcome"] == "urge_resisted")
        pending_checkins = sum(
            1 for r in rows if r["status"] == "sent" and r["followed"] is None
        )

        streak = 0
        for r in answered:
            if int(r["followed"]) == 1:
                streak += 1
            else:
                break

        prog_row = conn.execute("SELECT created_at FROM local_program WHERE id = 1").fetchone()
        earliest_dt = self._parse_iso(prog_row["created_at"]) if prog_row else now_utc
        if rows:
            oldest_log_dt = self._parse_iso(rows[-1]["triggered_at"])
            if oldest_log_dt and (earliest_dt is None or oldest_log_dt < earliest_dt):
                earliest_dt = oldest_log_dt

        day_number = max(1, (now_utc.date() - (earliest_dt or now_utc).date()).days + 1)
        if day_number <= 21:
            phase = "Phase 1: Synaptic Strengthening (Days 1 to 21)"
        elif day_number <= 56:
            phase = "Phase 2: Active Myelination (Weeks 3 to 8)"
        else:
            phase = "Phase 3: Effortless Default Baseline (Months 2 to 6)"

        stats = {
            "total_alerts_sent": total_sent,
            "total_checkins": total_checkins,
            "pending_checkins": pending_checkins,
            "followed_count": followed_count,
            "missed_count": missed_count,
            "already_relaxed_count": already_relaxed,
            "reset_now_count": reset_now,
            "urges_resisted_count": urges_resisted,
            "follow_rate_pct": (
                round(100.0 * followed_count / total_checkins, 1) if total_checkins else None
            ),
            "automaticity_rate_pct": (
                round(100.0 * already_relaxed / total_checkins, 1) if total_checkins else None
            ),
            "current_streak": streak,
            "day_number": day_number,
            "rewiring_phase": phase,
            "last_outcome": last_outcome,
        }
        return stats, last_outcome

    def _serialize_program_in_conn(
        self, conn: sqlite3.Connection, now_utc: datetime
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        row = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
        paused_until_dt = self._parse_iso(row["paused_until"])

        # Auto-expire temporary pause once paused_until passes
        if paused_until_dt is not None and paused_until_dt <= now_utc:
            next_run = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=float(row["current_interval_seconds"]),
                interval_mode=row["interval_mode"],
                jitter_pct=int(row["jitter_pct"]),
                timezone_name=row["timezone"],
                active_start_time=row["active_start_time"],
                active_end_time=row["active_end_time"],
                rng=self._rng,
            )
            conn.execute(
                """
                UPDATE local_program
                SET paused_until = NULL, pause_mode = NULL, next_run_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (next_run.isoformat(), now_utc.isoformat()),
            )
            row = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
            paused_until_dt = None

        stats, last_outcome = self._compute_stats_in_conn(conn, now_utc)
        start_secs = interval_to_seconds(float(row["interval_value"]), row["interval_unit"])
        tgt_secs = (
            interval_to_seconds(float(row["target_interval_value"]), row["target_interval_unit"])
            if int(row["progressive_enabled"]) == 1
            else None
        )
        cur_secs = float(row["current_interval_seconds"] or start_secs)

        coaching = LocalCoachingEngine.generate_prompt(
            stats=stats,
            last_outcome=last_outcome,
            current_interval_seconds=cur_secs,
            start_interval_seconds=start_secs,
            target_interval_seconds=tgt_secs,
            progressive_enabled=bool(row["progressive_enabled"]),
            adaptive_feedback=bool(row["adaptive_feedback"]),
            interval_mode=row["interval_mode"],
            jitter_pct=int(row["jitter_pct"]),
            run_count=int(row["run_count"]),
            exercises_enabled=False,
            rng=self._rng,
        )

        enabled = bool(row["enabled"])
        is_temp_paused = bool(enabled and paused_until_dt is not None and paused_until_dt > now_utc)
        if not enabled:
            status_state = "paused_indefinite"
        elif is_temp_paused:
            status_state = "paused_temp"
        else:
            status_state = "active"

        quiet_now = bool(
            status_state == "active"
            and is_in_quiet_hours(
                now_utc, row["timezone"], row["active_start_time"], row["active_end_time"]
            )
        )

        cur_disp = format_duration_seconds(cur_secs)
        tgt_disp = format_duration_seconds(tgt_secs) if tgt_secs else None
        mode_lbl = f"±{row['jitter_pct']}% random" if row["interval_mode"] == "random" else "fixed"
        sched_desc = (
            f"~{cur_disp} ({mode_lbl}) -> {tgt_disp} goal"
            if tgt_disp
            else f"Every {cur_disp} ({mode_lbl})"
        )

        program = {
            "id": 1,
            "title": coaching.title,
            "message": coaching.message,
            "coach_headline": coaching.coach_headline,
            "coach_note": coaching.coach_note,
            "stage_badge": coaching.stage_badge,
            "goal_progress_pct": coaching.goal_progress_pct,
            "goal_milestone": coaching.goal_milestone,
            "interval_mode": row["interval_mode"],
            "jitter_pct": int(row["jitter_pct"]),
            "interval_value": float(row["interval_value"]),
            "interval_unit": row["interval_unit"],
            "progressive_enabled": bool(row["progressive_enabled"]),
            "target_interval_value": float(row["target_interval_value"]),
            "target_interval_unit": row["target_interval_unit"],
            "progressive_step_type": row["progressive_step_type"],
            "progressive_step_value": float(row["progressive_step_value"]),
            "progressive_step_unit": row["progressive_step_unit"],
            "current_interval_seconds": cur_secs,
            "current_interval_display": cur_disp,
            "adaptive_feedback": bool(row["adaptive_feedback"]),
            "exercises_enabled": bool(row["exercises_enabled"]),
            "exercise_every_n": int(row["exercise_every_n"]),
            "active_start_time": row["active_start_time"],
            "active_end_time": row["active_end_time"],
            "timezone": row["timezone"],
            "sound_enabled": bool(row["sound_enabled"]),
            "enabled": enabled,
            "paused_until": paused_until_dt.isoformat() if is_temp_paused and paused_until_dt else None,
            "pause_mode": row["pause_mode"] if is_temp_paused else None,
            "notifications_active": status_state == "active",
            "is_temporarily_paused": is_temp_paused,
            "in_quiet_hours": quiet_now,
            "status_state": status_state,
            "schedule_description": sched_desc,
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "run_count": int(row["run_count"]),
        }
        return program, stats

    @staticmethod
    def _serialize_log(row: sqlite3.Row) -> Dict[str, Any]:
        followed_raw = row["followed"]
        followed = None if followed_raw is None else bool(int(followed_raw))
        outcome = row["outcome"]
        if outcome == "already_relaxed":
            display = 'Already in "N"'
        elif outcome == "reset_now":
            display = "Caught & Reset"
        elif outcome == "urge_resisted":
            display = "5s Urge Resisted"
        elif outcome == "missed":
            display = "Missed"
        elif row["status"] == "superseded" and followed is None:
            display = "Cleared"
        elif followed is None:
            display = "Unanswered"
        else:
            display = "Followed"

        return {
            "id": int(row["id"]),
            "client_uuid": row["client_uuid"],
            "cloud_log_id": (
                int(row["cloud_log_id"]) if row["cloud_log_id"] is not None else None
            ),
            "title": row["title"],
            "message": row["message"],
            "triggered_at": row["triggered_at"],
            "trigger_source": row["trigger_source"],
            "status": row["status"],
            "followed": followed,
            "outcome": outcome,
            "display_outcome": display,
            "note": row["note"] or "",
            "feedback_at": row["feedback_at"],
            "synced_to_cloud": bool(row["synced_to_cloud"]),
        }

    # --- Public Program & Schedule Operations ---

    def get_program_and_stats(self, now: Optional[datetime] = None) -> tuple[Dict[str, Any], Dict[str, Any]]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            return self._serialize_program_in_conn(conn, now_utc)

    def update_program(
        self, payload: Dict[str, Any], now: Optional[datetime] = None
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()

            mode = str(payload.get("interval_mode", cur["interval_mode"])).strip().lower()
            if mode not in ("random", "fixed"):
                raise ValueError("interval_mode must be 'random' or 'fixed'.")
            jitter = max(5, min(60, int(payload.get("jitter_pct", cur["jitter_pct"]))))

            iv_val = float(payload.get("interval_value", cur["interval_value"]))
            iv_unit = str(payload.get("interval_unit", cur["interval_unit"])).strip().lower()
            start_secs = interval_to_seconds(iv_val, iv_unit)

            prog_enabled = bool(payload.get("progressive_enabled", bool(cur["progressive_enabled"])))
            tgt_val = float(payload.get("target_interval_value", cur["target_interval_value"]))
            tgt_unit = str(payload.get("target_interval_unit", cur["target_interval_unit"])).strip().lower()
            if prog_enabled:
                interval_to_seconds(tgt_val, tgt_unit)

            adaptive = bool(payload.get("adaptive_feedback", bool(cur["adaptive_feedback"])))
            ex_enabled = bool(payload.get("exercises_enabled", bool(cur["exercises_enabled"])))
            ex_every_n = max(2, min(20, int(payload.get("exercise_every_n", cur["exercise_every_n"]))))

            act_start = validate_hhmm(
                payload.get("active_start_time", cur["active_start_time"]), "active_start_time"
            ) or "08:30"
            act_end = validate_hhmm(
                payload.get("active_end_time", cur["active_end_time"]), "active_end_time"
            ) or "21:30"
            tz_name = str(payload.get("timezone", cur["timezone"])).strip()
            validate_timezone(tz_name)

            sound_enabled = bool(payload.get("sound_enabled", bool(cur["sound_enabled"])))
            enabled = bool(payload.get("enabled", bool(cur["enabled"])))

            reset_cur = bool(payload.get("reset_interval", False))
            old_start = interval_to_seconds(float(cur["interval_value"]), cur["interval_unit"])
            cur_secs = (
                start_secs
                if (reset_cur or abs(old_start - start_secs) > 1.0 or not prog_enabled)
                else float(cur["current_interval_seconds"])
            )

            next_run = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=cur_secs,
                interval_mode=mode,
                jitter_pct=jitter,
                timezone_name=tz_name,
                active_start_time=act_start,
                active_end_time=act_end,
                rng=self._rng,
            )

            conn.execute(
                """
                UPDATE local_program SET
                    interval_mode = ?, jitter_pct = ?,
                    interval_value = ?, interval_unit = ?,
                    progressive_enabled = ?, target_interval_value = ?, target_interval_unit = ?,
                    current_interval_seconds = ?, adaptive_feedback = ?,
                    exercises_enabled = ?, exercise_every_n = ?,
                    active_start_time = ?, active_end_time = ?, timezone = ?,
                    sound_enabled = ?, enabled = ?, next_run_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    mode,
                    jitter,
                    iv_val,
                    iv_unit,
                    int(prog_enabled),
                    tgt_val,
                    tgt_unit,
                    cur_secs,
                    int(adaptive),
                    int(ex_enabled),
                    ex_every_n,
                    act_start,
                    act_end,
                    tz_name,
                    int(sound_enabled),
                    int(enabled),
                    next_run.isoformat(),
                    now_utc.isoformat(),
                ),
            )
            return self._serialize_program_in_conn(conn, now_utc)

    def pause_program(
        self,
        mode: str = "minutes",
        minutes: int = 60,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
            if mode == "indefinite":
                conn.execute(
                    """
                    UPDATE local_program
                    SET enabled = 0, paused_until = NULL, pause_mode = 'indefinite', updated_at = ?
                    WHERE id = 1
                    """,
                    (now_utc.isoformat(),),
                )
            elif mode == "until_morning":
                until_dt = next_awake_morning_utc(
                    now_utc, cur["timezone"], cur["active_start_time"]
                )
                conn.execute(
                    """
                    UPDATE local_program
                    SET enabled = 1, paused_until = ?, pause_mode = 'until_morning',
                        next_run_at = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (until_dt.isoformat(), until_dt.isoformat(), now_utc.isoformat()),
                )
            else:
                mins = max(1, int(minutes or 60))
                until_dt = now_utc + timedelta(minutes=mins)
                conn.execute(
                    """
                    UPDATE local_program
                    SET enabled = 1, paused_until = ?, pause_mode = 'minutes',
                        next_run_at = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (until_dt.isoformat(), until_dt.isoformat(), now_utc.isoformat()),
                )
            prog, _ = self._serialize_program_in_conn(conn, now_utc)
            return prog

    def resume_program(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
            next_run = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=float(cur["current_interval_seconds"]),
                interval_mode=cur["interval_mode"],
                jitter_pct=int(cur["jitter_pct"]),
                timezone_name=cur["timezone"],
                active_start_time=cur["active_start_time"],
                active_end_time=cur["active_end_time"],
                rng=self._rng,
            )
            conn.execute(
                """
                UPDATE local_program
                SET enabled = 1, paused_until = NULL, pause_mode = NULL,
                    next_run_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (next_run.isoformat(), now_utc.isoformat()),
            )
            prog, _ = self._serialize_program_in_conn(conn, now_utc)
            return prog

    def reset_interval(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        prog, _ = self.update_program({"reset_interval": True}, now=now)
        return prog

    def restore_defaults(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        payload = {**DEFAULT_PROGRAM, "reset_interval": True}
        prog, _ = self.update_program(payload, now=now)
        return prog

    # --- Scheduler Firing & Check-In Feedback ---

    def _fire_nudge_in_conn(
        self,
        conn: sqlite3.Connection,
        now_utc: datetime,
        *,
        trigger_source: str = "scheduled",
        force_exercise: bool = False,
    ) -> Dict[str, Any]:
        # Penalty-free auto-supersede any previous unanswered check-in banner
        conn.execute(
            """
            UPDATE local_logs
            SET status = 'superseded'
            WHERE followed IS NULL AND status = 'sent'
            """
        )

        row = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
        stats, last_outcome = self._compute_stats_in_conn(conn, now_utc)
        start_secs = interval_to_seconds(float(row["interval_value"]), row["interval_unit"])
        tgt_secs = (
            interval_to_seconds(float(row["target_interval_value"]), row["target_interval_unit"])
            if int(row["progressive_enabled"]) == 1
            else None
        )
        cur_secs = float(row["current_interval_seconds"] or start_secs)
        new_run_count = int(row["run_count"]) + 1

        prompt = LocalCoachingEngine.generate_prompt(
            stats=stats,
            last_outcome=last_outcome,
            current_interval_seconds=cur_secs,
            start_interval_seconds=start_secs,
            target_interval_seconds=tgt_secs,
            progressive_enabled=bool(row["progressive_enabled"]),
            adaptive_feedback=bool(row["adaptive_feedback"]),
            interval_mode=row["interval_mode"],
            jitter_pct=int(row["jitter_pct"]),
            run_count=new_run_count,
            exercises_enabled=bool(row["exercises_enabled"]),
            exercise_every_n=int(row["exercise_every_n"]),
            force_exercise=force_exercise,
            rng=self._rng,
        )

        next_run = compute_next_interval_run(
            now_utc=now_utc,
            base_interval_seconds=cur_secs,
            interval_mode=row["interval_mode"],
            jitter_pct=int(row["jitter_pct"]),
            timezone_name=row["timezone"],
            active_start_time=row["active_start_time"],
            active_end_time=row["active_end_time"],
            rng=self._rng,
        )

        client_uuid = str(uuid.uuid4())
        cursor = conn.execute(
            """
            INSERT INTO local_logs (
                client_uuid, title, message, triggered_at, trigger_source,
                status, interval_seconds_at_trigger
            ) VALUES (?, ?, ?, ?, ?, 'sent', ?)
            """,
            (
                client_uuid,
                prompt.title,
                prompt.message,
                now_utc.isoformat(),
                trigger_source,
                cur_secs,
            ),
        )
        log_id = int(cursor.lastrowid)

        conn.execute(
            """
            UPDATE local_program
            SET title = ?, message = ?, last_run_at = ?, next_run_at = ?,
                run_count = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                prompt.title,
                prompt.message,
                now_utc.isoformat(),
                next_run.isoformat(),
                new_run_count,
                now_utc.isoformat(),
            ),
        )
        log_row = conn.execute("SELECT * FROM local_logs WHERE id = ?", (log_id,)).fetchone()
        return self._serialize_log(log_row)

    def due_tick(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Check if the active schedule is due at `now` and fire a nudge if outside quiet hours."""
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            prog, _ = self._serialize_program_in_conn(conn, now_utc)
            if not prog["notifications_active"]:
                return None

            if prog["in_quiet_hours"]:
                snapped = compute_next_interval_run(
                    now_utc=now_utc,
                    base_interval_seconds=MIN_INTERVAL_SECONDS,
                    interval_mode="fixed",
                    timezone_name=prog["timezone"],
                    active_start_time=prog["active_start_time"],
                    active_end_time=prog["active_end_time"],
                )
                conn.execute(
                    "UPDATE local_program SET next_run_at = ?, updated_at = ? WHERE id = 1",
                    (snapped.isoformat(), now_utc.isoformat()),
                )
                return None

            next_run_dt = self._parse_iso(prog["next_run_at"])
            if next_run_dt is not None and now_utc < next_run_dt:
                return None

            return self._fire_nudge_in_conn(conn, now_utc, trigger_source="scheduled")

    def trigger_now(
        self,
        *,
        force_exercise: bool = False,
        trigger_source: str = "manual",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            return self._fire_nudge_in_conn(
                conn,
                now_utc,
                trigger_source=trigger_source,
                force_exercise=force_exercise,
            )

    def record_feedback(
        self,
        log_id: int,
        outcome: str,
        note: str = "",
        now: Optional[datetime] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        outcome_key = outcome.strip().lower()
        if outcome_key not in ("already_relaxed", "reset_now", "urge_resisted", "missed"):
            raise ValueError(f"Unsupported check-in outcome '{outcome}'.")

        followed = 0 if outcome_key == "missed" else 1
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM local_logs WHERE id = ?", (int(log_id),)).fetchone()
            if row is None:
                raise ValueError(f"Log #{log_id} not found.")

            new_status = "recorded" if row["trigger_source"] == "self_report" else (
                "sent" if row["status"] in ("sent", "superseded") else row["status"]
            )
            conn.execute(
                """
                UPDATE local_logs
                SET followed = ?, outcome = ?, note = ?, feedback_at = ?,
                    status = ?, synced_to_cloud = 0
                WHERE id = ?
                """,
                (
                    followed,
                    outcome_key,
                    (note or "").strip()[:500],
                    now_utc.isoformat(),
                    new_status,
                    int(log_id),
                ),
            )

            prog_row = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()
            cur_secs = float(prog_row["current_interval_seconds"])
            start_secs = interval_to_seconds(
                float(prog_row["interval_value"]), prog_row["interval_unit"]
            )
            if int(prog_row["progressive_enabled"]) == 1:
                tgt_secs = interval_to_seconds(
                    float(prog_row["target_interval_value"]), prog_row["target_interval_unit"]
                )
                if outcome_key == "already_relaxed":
                    cur_secs = step_progressive_interval(
                        cur_secs,
                        tgt_secs,
                        prog_row["progressive_step_type"],
                        float(prog_row["progressive_step_value"]),
                        prog_row["progressive_step_unit"],
                    )
                elif int(prog_row["adaptive_feedback"]) == 1 and outcome_key in ("missed", "reset_now"):
                    factor = 0.85 if outcome_key == "missed" else 0.95
                    cur_secs = max(MIN_INTERVAL_SECONDS, min(cur_secs, max(start_secs * 0.75, cur_secs * factor)))

            next_run = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=cur_secs,
                interval_mode=prog_row["interval_mode"],
                jitter_pct=int(prog_row["jitter_pct"]),
                timezone_name=prog_row["timezone"],
                active_start_time=prog_row["active_start_time"],
                active_end_time=prog_row["active_end_time"],
                rng=self._rng,
            )
            conn.execute(
                """
                UPDATE local_program
                SET current_interval_seconds = ?, next_run_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (round(cur_secs, 2), next_run.isoformat(), now_utc.isoformat()),
            )

            updated_log = self._serialize_log(
                conn.execute("SELECT * FROM local_logs WHERE id = ?", (int(log_id),)).fetchone()
            )
            program, stats = self._serialize_program_in_conn(conn, now_utc)
            return updated_log, program, stats

    def record_quick_urge(
        self, note: str = "", now: Optional[datetime] = None
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO local_logs (
                    client_uuid, title, message, triggered_at, trigger_source,
                    status, followed, outcome, note, feedback_at
                ) VALUES (?, ?, ?, ?, 'self_report', 'recorded', 1, 'urge_resisted', ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "5s Urge Resisted (Self-Check)",
                    "Self-logged 5-second clench urge resisted, starving the old pathway.",
                    now_utc.isoformat(),
                    (note or "").strip()[:500],
                    now_utc.isoformat(),
                ),
            )
            log_row = conn.execute(
                "SELECT * FROM local_logs WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
            stats, _ = self._compute_stats_in_conn(conn, now_utc)
            return self._serialize_log(log_row), stats

    def clear_unanswered_checkins(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Penalty-free bulk clear of unanswered nudges (`status = 'superseded'`)."""
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE local_logs
                SET status = 'superseded'
                WHERE followed IS NULL AND status = 'sent'
                """
            )
            updated_count = int(cur.rowcount or 0)
            program, stats = self._serialize_program_in_conn(conn, now_utc)
            return {
                "cleared": True,
                "updated_count": updated_count,
                "program": program,
                "stats": stats,
            }

    def latest_pending_log_id(self) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM local_logs
                WHERE followed IS NULL AND status = 'sent'
                ORDER BY triggered_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            return int(row["id"]) if row else None

    # --- Cloud Sync Hydration & Pending Mutation Helpers ---

    def get_unsynced_cloud_mutations(self, limit: int = 100) -> Dict[str, List[Dict[str, Any]]]:
        """Return local 5s urges and locally-answered cloud nudges not yet uploaded."""
        cap = max(1, min(500, int(limit)))
        with self._connect() as conn:
            urges = conn.execute(
                """
                SELECT id, note, triggered_at FROM local_logs
                WHERE synced_to_cloud = 0
                  AND cloud_log_id IS NULL
                  AND trigger_source = 'self_report'
                  AND outcome = 'urge_resisted'
                ORDER BY triggered_at ASC, id ASC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()

            feedback_rows = conn.execute(
                """
                SELECT id, cloud_log_id, outcome, note FROM local_logs
                WHERE synced_to_cloud = 0
                  AND cloud_log_id IS NOT NULL
                  AND followed IS NOT NULL
                  AND outcome IS NOT NULL
                ORDER BY feedback_at ASC, id ASC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()

            return {
                "urges": [
                    {
                        "local_id": int(r["id"]),
                        "note": r["note"] or "",
                        "triggered_at": r["triggered_at"],
                    }
                    for r in urges
                ],
                "feedback": [
                    {
                        "local_id": int(r["id"]),
                        "cloud_log_id": int(r["cloud_log_id"]),
                        "outcome": str(r["outcome"]),
                        "note": r["note"] or "",
                    }
                    for r in feedback_rows
                ],
            }

    def mark_urge_synced(self, local_id: int, cloud_log_id: Optional[int] = None) -> None:
        with self._connect() as conn:
            if cloud_log_id is not None:
                cid = int(cloud_log_id)
                conn.execute(
                    """
                    UPDATE local_logs
                    SET synced_to_cloud = 1, cloud_log_id = ?, client_uuid = ?
                    WHERE id = ?
                    """,
                    (cid, f"cloud:{cid}", int(local_id)),
                )
            else:
                conn.execute(
                    "UPDATE local_logs SET synced_to_cloud = 1 WHERE id = ?",
                    (int(local_id),),
                )

    def mark_feedback_synced(self, local_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE local_logs SET synced_to_cloud = 1 WHERE id = ?",
                (int(local_id),),
            )

    def _apply_cloud_program_in_conn(
        self,
        conn: sqlite3.Connection,
        cloud_program: Dict[str, Any],
        now_utc: datetime,
        fallback_created_at: Optional[str] = None,
    ) -> None:
        if not cloud_program:
            return
        cur = conn.execute("SELECT * FROM local_program WHERE id = 1").fetchone()

        title = str(cloud_program.get("title") or cur["title"])
        message = str(cloud_program.get("message") or cur["message"])

        mode = str(cloud_program.get("interval_mode") or cur["interval_mode"]).strip().lower()
        if mode not in ("random", "fixed"):
            mode = "random"
        jitter = max(5, min(60, int(cloud_program.get("jitter_pct") or cur["jitter_pct"] or 25)))

        iv_val = float(cloud_program.get("interval_value") or cur["interval_value"] or 20.0)
        iv_unit = str(cloud_program.get("interval_unit") or cur["interval_unit"] or "minutes").strip().lower()
        if iv_unit not in ("seconds", "minutes", "hours", "days"):
            iv_unit = "minutes"
        start_secs = interval_to_seconds(iv_val, iv_unit)

        prog_enabled = bool(cloud_program.get("progressive_enabled", bool(cur["progressive_enabled"])))
        tgt_val = float(
            cloud_program.get("target_interval_value") or cur["target_interval_value"] or 90.0
        )
        tgt_unit = str(
            cloud_program.get("target_interval_unit") or cur["target_interval_unit"] or "minutes"
        ).strip().lower()
        if tgt_unit not in ("seconds", "minutes", "hours", "days"):
            tgt_unit = "minutes"

        step_type = str(
            cloud_program.get("progressive_step_type") or cur["progressive_step_type"] or "linear"
        ).strip().lower()
        if step_type not in ("linear", "percent"):
            step_type = "linear"
        step_val = float(
            cloud_program.get("progressive_step_value") or cur["progressive_step_value"] or 5.0
        )
        step_unit = str(
            cloud_program.get("progressive_step_unit") or cur["progressive_step_unit"] or "minutes"
        ).strip().lower()
        if step_unit not in ("seconds", "minutes", "hours", "days"):
            step_unit = "minutes"

        raw_cur_secs = cloud_program.get("current_interval_seconds")
        cur_secs = (
            max(MIN_INTERVAL_SECONDS, float(raw_cur_secs))
            if raw_cur_secs is not None
            else start_secs
        )

        adaptive = bool(cloud_program.get("adaptive_feedback", bool(cur["adaptive_feedback"])))
        ex_enabled = bool(cloud_program.get("exercises_enabled", bool(cur["exercises_enabled"])))
        ex_every_n = max(
            2,
            min(
                20,
                int(
                    cloud_program.get("exercise_every_n")
                    or cur["exercise_every_n"]
                    or DEFAULT_EXERCISE_EVERY_N
                ),
            ),
        )

        act_start = (
            validate_hhmm(cloud_program.get("active_start_time"), "active_start_time")
            or cur["active_start_time"]
            or "08:30"
        )
        act_end = (
            validate_hhmm(cloud_program.get("active_end_time"), "active_end_time")
            or cur["active_end_time"]
            or "21:30"
        )
        tz_candidate = str(
            cloud_program.get("timezone") or cur["timezone"] or "America/New_York"
        ).strip()
        try:
            validate_timezone(tz_candidate)
            tz_name = tz_candidate
        except ValueError:
            tz_name = str(cur["timezone"])

        enabled = bool(cloud_program.get("enabled", bool(cur["enabled"])))
        paused_until_dt = self._parse_iso(cloud_program.get("paused_until"))
        paused_until_iso = (
            paused_until_dt.isoformat()
            if (paused_until_dt is not None and paused_until_dt > now_utc)
            else None
        )
        raw_pause_mode = cloud_program.get("pause_mode")
        pause_mode = (
            str(raw_pause_mode).strip()
            if raw_pause_mode and (not enabled or paused_until_iso is not None)
            else ("indefinite" if not enabled else None)
        )

        last_run_dt = self._parse_iso(cloud_program.get("last_run_at"))
        last_run_iso = last_run_dt.isoformat() if last_run_dt else cur["last_run_at"]

        next_run_dt = self._parse_iso(cloud_program.get("next_run_at"))
        if next_run_dt is not None and next_run_dt > now_utc:
            next_run_iso = next_run_dt.isoformat()
        else:
            next_run_iso = compute_next_interval_run(
                now_utc=now_utc,
                base_interval_seconds=cur_secs,
                interval_mode=mode,
                jitter_pct=jitter,
                timezone_name=tz_name,
                active_start_time=act_start,
                active_end_time=act_end,
                rng=self._rng,
            ).isoformat()

        run_count = (
            max(0, int(cloud_program["run_count"]))
            if cloud_program.get("run_count") is not None
            else int(cur["run_count"] or 0)
        )

        created_dt = self._parse_iso(cloud_program.get("created_at") or fallback_created_at)
        created_iso = created_dt.isoformat() if created_dt else str(cur["created_at"])

        conn.execute(
            """
            UPDATE local_program SET
                title = ?, message = ?,
                interval_mode = ?, jitter_pct = ?,
                interval_value = ?, interval_unit = ?,
                progressive_enabled = ?, target_interval_value = ?, target_interval_unit = ?,
                progressive_step_type = ?, progressive_step_value = ?, progressive_step_unit = ?,
                current_interval_seconds = ?, adaptive_feedback = ?,
                exercises_enabled = ?, exercise_every_n = ?,
                active_start_time = ?, active_end_time = ?, timezone = ?,
                enabled = ?, paused_until = ?, pause_mode = ?,
                next_run_at = ?, last_run_at = ?, run_count = ?,
                created_at = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                title,
                message,
                mode,
                jitter,
                iv_val,
                iv_unit,
                int(prog_enabled),
                tgt_val,
                tgt_unit,
                step_type,
                step_val,
                step_unit,
                round(cur_secs, 2),
                int(adaptive),
                int(ex_enabled),
                ex_every_n,
                act_start,
                act_end,
                tz_name,
                int(enabled),
                paused_until_iso,
                pause_mode,
                next_run_iso,
                last_run_iso,
                run_count,
                created_iso,
                now_utc.isoformat(),
            ),
        )

    def apply_cloud_program(
        self, cloud_program: Dict[str, Any], now: Optional[datetime] = None
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            self._apply_cloud_program_in_conn(conn, cloud_program, now_utc)
            return self._serialize_program_in_conn(conn, now_utc)

    def apply_cloud_snapshot(
        self,
        *,
        user: Optional[Dict[str, Any]] = None,
        cloud_program: Optional[Dict[str, Any]] = None,
        cloud_logs: Optional[List[Dict[str, Any]]] = None,
        is_complete_log_snapshot: bool = True,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Hydrate `local_logs`, `local_program`, and `settings['theme']` idempotently from
        `declench.app` without resetting progression or duplicating check-in rows.
        """
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)
        imported_count = 0
        updated_count = 0
        seen_cloud_ids: set[int] = set()

        with self._connect() as conn:
            if user and isinstance(user.get("theme"), str):
                theme_key = user["theme"].strip().lower()
                if theme_key in VALID_THEME_KEYS:
                    conn.execute(
                        """
                        INSERT INTO settings (key, value) VALUES ('theme', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (theme_key,),
                    )

            if cloud_logs is not None:
                existing_rows = conn.execute(
                    "SELECT id, cloud_log_id, synced_to_cloud, followed FROM local_logs WHERE cloud_log_id IS NOT NULL"
                ).fetchall()
                existing_by_cloud_id = {
                    int(r["cloud_log_id"]): r for r in existing_rows
                }

                for item in cloud_logs:
                    raw_id = item.get("id")
                    if raw_id is None:
                        continue
                    cloud_id = int(raw_id)
                    triggered_dt = self._parse_iso(
                        item.get("triggered_at_utc") or item.get("triggered_at")
                    )
                    if triggered_dt is None:
                        continue
                    seen_cloud_ids.add(cloud_id)

                    title = str(
                        item.get("title") or item.get("reminder_title") or "Declench Posture Coach"
                    ).strip()
                    raw_msg = (
                        item.get("message")
                        if "message" in item
                        else item.get("reminder_message")
                    )
                    message = str(raw_msg or "").strip()
                    trigger_source = str(item.get("trigger_source") or "scheduled").strip().lower()
                    status = str(
                        item.get("delivery_status") or item.get("status") or "sent"
                    ).strip().lower()

                    followed_val = item.get("followed")
                    followed_int = (
                        None if followed_val is None else (1 if bool(followed_val) else 0)
                    )
                    raw_outcome = str(
                        item.get("feedback_outcome") or item.get("outcome") or ""
                    ).strip().lower()
                    if followed_int is None or raw_outcome == "unanswered":
                        outcome = None
                    else:
                        outcome = raw_outcome or ("followed" if followed_int == 1 else "missed")

                    raw_note = (
                        item.get("feedback_note")
                        if "feedback_note" in item
                        else item.get("note")
                    )
                    note = str(raw_note or "").strip()[:500]
                    fb_dt = self._parse_iso(
                        item.get("feedback_at_utc") or item.get("feedback_at")
                    )
                    fb_iso = fb_dt.isoformat() if fb_dt else None

                    raw_cadence = (
                        item.get("cadence_seconds")
                        if "cadence_seconds" in item
                        else item.get("interval_seconds_at_trigger")
                    )
                    cadence_secs = (
                        float(raw_cadence)
                        if raw_cadence is not None and raw_cadence != ""
                        else None
                    )

                    existing = existing_by_cloud_id.get(cloud_id)
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO local_logs (
                                client_uuid, cloud_log_id, title, message, triggered_at,
                                trigger_source, status, followed, outcome, note,
                                feedback_at, interval_seconds_at_trigger, synced_to_cloud
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                f"cloud:{cloud_id}",
                                cloud_id,
                                title,
                                message,
                                triggered_dt.isoformat(),
                                trigger_source,
                                status,
                                followed_int,
                                outcome,
                                note,
                                fb_iso,
                                cadence_secs,
                            ),
                        )
                        imported_count += 1
                    else:
                        # Preserve any unsynced local feedback until uploaded
                        if int(existing["synced_to_cloud"]) == 0 and existing["followed"] is not None:
                            continue
                        conn.execute(
                            """
                            UPDATE local_logs SET
                                title = ?, message = ?, triggered_at = ?,
                                trigger_source = ?, status = ?, followed = ?,
                                outcome = ?, note = ?, feedback_at = ?,
                                interval_seconds_at_trigger = ?, synced_to_cloud = 1
                            WHERE id = ?
                            """,
                            (
                                title,
                                message,
                                triggered_dt.isoformat(),
                                trigger_source,
                                status,
                                followed_int,
                                outcome,
                                note,
                                fb_iso,
                                cadence_secs,
                                int(existing["id"]),
                            ),
                        )
                        updated_count += 1

                if is_complete_log_snapshot:
                    pending_cloud_rows = conn.execute(
                        """
                        SELECT id, cloud_log_id FROM local_logs
                        WHERE cloud_log_id IS NOT NULL
                          AND followed IS NULL
                          AND status = 'sent'
                        """
                    ).fetchall()
                    for prow in pending_cloud_rows:
                        if int(prow["cloud_log_id"]) not in seen_cloud_ids:
                            conn.execute(
                                "UPDATE local_logs SET status = 'superseded' WHERE id = ?",
                                (int(prow["id"]),),
                            )

                    cloud_test_rows = conn.execute(
                        """
                        SELECT id, cloud_log_id FROM local_logs
                        WHERE cloud_log_id IS NOT NULL AND trigger_source = 'test'
                        """
                    ).fetchall()
                    for trow in cloud_test_rows:
                        if int(trow["cloud_log_id"]) not in seen_cloud_ids:
                            conn.execute(
                                "DELETE FROM local_logs WHERE id = ?",
                                (int(trow["id"]),),
                            )

            if cloud_program:
                self._apply_cloud_program_in_conn(
                    conn,
                    cloud_program,
                    now_utc,
                    fallback_created_at=(user or {}).get("created_at"),
                )

            program, stats = self._serialize_program_in_conn(conn, now_utc)
            return {
                "imported_logs": imported_count,
                "updated_logs": updated_count,
                "program": program,
                "stats": stats,
            }

    def list_logs(
        self,
        *,
        limit: int = 50,
        outcome: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["(status IN ('sent', 'recorded') OR followed IS NOT NULL)"]
        params: List[Any] = []

        out_filter = (outcome or "").strip().lower()
        if out_filter and out_filter != "all":
            if out_filter in ("unanswered", "pending"):
                clauses.append("followed IS NULL AND status = 'sent'")
            else:
                clauses.append("outcome = ?")
                params.append(out_filter)

        q = (search or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append("(title LIKE ? OR message LIKE ? OR note LIKE ? OR outcome LIKE ?)")
            params.extend([like, like, like, like])

        where_sql = " AND ".join(clauses)
        params.append(max(1, min(1000, int(limit))))

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM local_logs
                WHERE {where_sql}
                ORDER BY triggered_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._serialize_log(r) for r in rows]

    # --- Local 24h / 14d / 12m Analytics Timeseries ---

    def get_timeseries(
        self, granularity: str = "daily", now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        gran = (granularity or "daily").strip().lower()
        if gran not in ("hourly", "daily", "monthly"):
            gran = "daily"
        now_utc = ensure_utc(now) or datetime.now(timezone.utc)

        with self._connect() as conn:
            prog_row = conn.execute("SELECT timezone FROM local_program WHERE id = 1").fetchone()
            tz_name = prog_row["timezone"] if prog_row else "America/New_York"
            try:
                tz_obj: Any = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz_obj = timezone.utc

            now_local = now_utc.astimezone(tz_obj)
            buckets_by_key: Dict[str, Dict[str, Any]] = {}
            ordered_keys: List[str] = []

            if gran == "hourly":
                start_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
                cutoff_utc = start_hour
                for i in range(24):
                    b_utc = start_hour + timedelta(hours=i)
                    b_loc = b_utc.astimezone(tz_obj)
                    key = b_utc.isoformat()
                    hr12 = b_loc.hour % 12 or 12
                    ampm = "a" if b_loc.hour < 12 else "p"
                    ordered_keys.append(key)
                    buckets_by_key[key] = {
                        "key": key,
                        "short_label": f"{hr12}{ampm}",
                        "full_label": b_loc.strftime("%a %I:00 %p"),
                        "already_relaxed": 0,
                        "reset_now": 0,
                        "urge_resisted": 0,
                        "followed_other": 0,
                        "missed": 0,
                        "unanswered": 0,
                    }
            elif gran == "daily":
                start_date = now_local.date() - timedelta(days=13)
                cutoff_utc = datetime(
                    start_date.year, start_date.month, start_date.day, 0, 0, tzinfo=tz_obj
                ).astimezone(timezone.utc) - timedelta(hours=2)
                for i in range(14):
                    d = start_date + timedelta(days=i)
                    key = d.isoformat()
                    ordered_keys.append(key)
                    buckets_by_key[key] = {
                        "key": key,
                        "short_label": f"{d.strftime('%b')} {d.day}",
                        "full_label": d.strftime("%a, %b %d"),
                        "already_relaxed": 0,
                        "reset_now": 0,
                        "urge_resisted": 0,
                        "followed_other": 0,
                        "missed": 0,
                        "unanswered": 0,
                    }
            else:
                cur_months = now_local.year * 12 + (now_local.month - 1)
                first_months = cur_months - 11
                fy, fm0 = divmod(first_months, 12)
                cutoff_utc = datetime(fy, fm0 + 1, 1, 0, 0, tzinfo=tz_obj).astimezone(
                    timezone.utc
                ) - timedelta(days=2)
                for i in range(12):
                    y, m0 = divmod(first_months + i, 12)
                    m = m0 + 1
                    ref = datetime(y, m, 1)
                    key = f"{y:04d}-{m:02d}"
                    ordered_keys.append(key)
                    buckets_by_key[key] = {
                        "key": key,
                        "short_label": ref.strftime("%b"),
                        "full_label": ref.strftime("%B %Y"),
                        "already_relaxed": 0,
                        "reset_now": 0,
                        "urge_resisted": 0,
                        "followed_other": 0,
                        "missed": 0,
                        "unanswered": 0,
                    }

            rows = conn.execute(
                "SELECT * FROM local_logs WHERE triggered_at >= ? ORDER BY triggered_at ASC",
                (cutoff_utc.isoformat(),),
            ).fetchall()

            for r in rows:
                if r["followed"] is None and r["status"] != "sent":
                    continue
                ts = self._parse_iso(r["triggered_at"])
                if ts is None:
                    continue
                if gran == "hourly":
                    b_key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
                elif gran == "daily":
                    b_key = ts.astimezone(tz_obj).date().isoformat()
                else:
                    loc = ts.astimezone(tz_obj)
                    b_key = f"{loc.year:04d}-{loc.month:02d}"

                bucket = buckets_by_key.get(b_key)
                if not bucket:
                    continue
                if r["followed"] is None:
                    bucket["unanswered"] += 1
                else:
                    out = (r["outcome"] or "").strip().lower()
                    if out in ("already_relaxed", "reset_now", "urge_resisted"):
                        bucket[out] += 1
                    elif int(r["followed"]) == 1:
                        bucket["followed_other"] += 1
                    else:
                        bucket["missed"] += 1

            buckets: List[Dict[str, Any]] = []
            max_total = 0
            peak_bucket: Optional[Dict[str, Any]] = None
            peak_tension = 0

            for k in ordered_keys:
                b = buckets_by_key[k]
                total = (
                    b["already_relaxed"]
                    + b["reset_now"]
                    + b["urge_resisted"]
                    + b["followed_other"]
                    + b["missed"]
                    + b["unanswered"]
                )
                tension = b["reset_now"] + b["urge_resisted"] + b["missed"]
                b["total"] = total
                buckets.append(b)
                if total > max_total:
                    max_total = total
                if tension > peak_tension:
                    peak_tension = tension
                    peak_bucket = {
                        "label": b["full_label"],
                        "tension_count": tension,
                    }

            window_labels = {
                "hourly": "Last 24 Hours",
                "daily": "Last 14 Days",
                "monthly": "Last 12 Months",
            }
            return {
                "granularity": gran,
                "window_label": window_labels[gran],
                "timezone": tz_name,
                "max_bucket_total": max_total,
                "buckets": buckets,
                "peak_tension": peak_bucket,
            }

    # --- CSV & JSON Local Data Export ---

    def export_csv(self) -> str:
        logs = self.list_logs(limit=1000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "triggered_at", "trigger_source", "outcome", "display_outcome", "title", "note"]
        )
        for item in logs:
            writer.writerow(
                [
                    item["id"],
                    item["triggered_at"],
                    item["trigger_source"],
                    item["outcome"] or "",
                    item["display_outcome"],
                    item["title"],
                    item["note"],
                ]
            )
        return buf.getvalue()

    def export_json(self) -> str:
        program, stats = self.get_program_and_stats()
        logs = self.list_logs(limit=1000)
        return json.dumps(
            {"program": program, "stats": stats, "logs": logs},
            indent=2,
            ensure_ascii=False,
        )

