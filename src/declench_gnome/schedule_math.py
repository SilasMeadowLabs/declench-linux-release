from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path
import random
import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VALID_INTERVAL_UNITS = ("seconds", "minutes", "hours", "days")
VALID_INTERVAL_MODES = ("fixed", "random")
VALID_PROGRESSIVE_STEP_TYPES = ("linear", "percent")
DEFAULT_JITTER_PCT = 25
MIN_JITTER_PCT = 5
MAX_JITTER_PCT = 60
MIN_INTERVAL_SECONDS = 10.0

UNIT_SECONDS = {
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3600.0,
    "days": 86400.0,
}

TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def detect_system_timezone() -> str:
    env_tz = (os.environ.get("TZ") or "").strip()
    if env_tz:
        try:
            ZoneInfo(env_tz)
            return env_tz
        except (ZoneInfoNotFoundError, ValueError):
            pass

    tz_file = Path("/etc/timezone")
    if tz_file.is_file():
        candidate = tz_file.read_text(encoding="utf-8", errors="ignore").strip()
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                pass

    localtime = Path("/etc/localtime")
    if localtime.exists():
        try:
            resolved = str(localtime.resolve())
            marker = "/zoneinfo/"
            if marker in resolved:
                candidate = resolved.split(marker, 1)[1].strip()
                ZoneInfo(candidate)
                return candidate
        except (OSError, ZoneInfoNotFoundError, ValueError):
            pass

    return "America/New_York"


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_timezone(tz_name: str) -> ZoneInfo:
    if not tz_name or not isinstance(tz_name, str):
        raise ValueError("Timezone is required.")
    try:
        return ZoneInfo(tz_name.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: '{tz_name}'") from exc


def validate_hhmm(value: Optional[str], field_name: str = "time") -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if not TIME_HHMM_RE.match(cleaned):
        raise ValueError(f"{field_name} must be in HH:MM (24-hour) format.")
    return cleaned


def parse_hhmm(value: str) -> time:
    m = TIME_HHMM_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid HH:MM time: '{value}'")
    return time(int(m.group(1)), int(m.group(2)))


def interval_to_seconds(value: float, unit: str) -> float:
    if unit not in UNIT_SECONDS:
        raise ValueError(f"Unsupported interval unit '{unit}'.")
    secs = float(value) * UNIT_SECONDS[unit]
    if secs < MIN_INTERVAL_SECONDS:
        raise ValueError(f"Interval must be at least {int(MIN_INTERVAL_SECONDS)} seconds.")
    return secs


def format_duration_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = max(1, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        mins, rem = divmod(total, 60)
        return f"{mins}m {rem}s" if rem else f"{mins}m"
    if total < 86400:
        hrs, rem = divmod(total, 3600)
        mins = rem // 60
        return f"{hrs}h {mins}m" if mins else f"{hrs}h"
    days, rem = divmod(total, 86400)
    hrs = rem // 3600
    return f"{days}d {hrs}h" if hrs else f"{days}d"


def step_progressive_interval(
    current_seconds: float,
    target_seconds: float,
    step_type: str = "linear",
    step_value: float = 2.0,
    step_unit: str = "minutes",
) -> float:
    target = max(MIN_INTERVAL_SECONDS, float(target_seconds))
    current = max(MIN_INTERVAL_SECONDS, float(current_seconds))

    if abs(current - target) < 0.5:
        return target

    if step_type == "percent":
        pct = max(0.5, min(95.0, float(step_value))) / 100.0
        if current > target:
            nxt = max(target, current * (1.0 - pct))
        else:
            nxt = min(target, current * (1.0 + pct))
    else:
        delta = max(1.0, float(step_value) * UNIT_SECONDS.get(step_unit or "minutes", 60.0))
        if current > target:
            nxt = max(target, current - delta)
        else:
            nxt = min(target, current + delta)

    return max(MIN_INTERVAL_SECONDS, round(nxt, 2))


def apply_random_jitter(
    base_seconds: float,
    jitter_pct: int = DEFAULT_JITTER_PCT,
    rng: Optional[random.Random] = None,
) -> float:
    base = max(MIN_INTERVAL_SECONDS, float(base_seconds))
    pct = max(0, min(MAX_JITTER_PCT, int(jitter_pct)))
    if pct <= 0:
        return round(base, 2)
    frac = pct / 100.0
    chooser = rng if rng is not None else random
    factor = chooser.uniform(1.0 - frac, 1.0 + frac)
    return max(MIN_INTERVAL_SECONDS, round(base * factor, 2))


def apply_active_hours(
    candidate_utc: datetime,
    timezone_name: str,
    active_start_time: Optional[str],
    active_end_time: Optional[str],
) -> datetime:
    if not active_start_time or not active_end_time:
        return candidate_utc

    tz = validate_timezone(timezone_name)
    start_t = parse_hhmm(active_start_time)
    end_t = parse_hhmm(active_end_time)
    if start_t == end_t:
        return candidate_utc

    local_dt = ensure_utc(candidate_utc).astimezone(tz)
    cur_t = local_dt.timetz().replace(tzinfo=None)

    if start_t < end_t:
        if cur_t < start_t:
            snapped = datetime(
                local_dt.year, local_dt.month, local_dt.day, start_t.hour, start_t.minute, 0, tzinfo=tz
            )
            return snapped.astimezone(timezone.utc)
        if cur_t > end_t:
            next_day = local_dt.date() + timedelta(days=1)
            snapped = datetime(
                next_day.year, next_day.month, next_day.day, start_t.hour, start_t.minute, 0, tzinfo=tz
            )
            return snapped.astimezone(timezone.utc)
        return candidate_utc

    if cur_t >= start_t or cur_t <= end_t:
        return candidate_utc

    snapped = datetime(
        local_dt.year, local_dt.month, local_dt.day, start_t.hour, start_t.minute, 0, tzinfo=tz
    )
    return snapped.astimezone(timezone.utc)


def is_in_quiet_hours(
    moment_utc: datetime,
    timezone_name: str,
    active_start_time: Optional[str],
    active_end_time: Optional[str],
) -> bool:
    if not active_start_time or not active_end_time:
        return False
    try:
        norm = ensure_utc(moment_utc)
        if norm is None:
            return False
        shifted = apply_active_hours(norm, timezone_name, active_start_time, active_end_time)
        return shifted > norm
    except ValueError:
        return False


def next_awake_morning_utc(
    now_utc: datetime,
    timezone_name: str,
    active_start_time: Optional[str] = None,
) -> datetime:
    tz = validate_timezone(timezone_name)
    wake_t = parse_hhmm(active_start_time or "08:00")
    local_now = ensure_utc(now_utc).astimezone(tz)
    cur_t = local_now.timetz().replace(tzinfo=None)
    today_morning = datetime(
        local_now.year, local_now.month, local_now.day, wake_t.hour, wake_t.minute, 0, tzinfo=tz
    )
    if cur_t < time(4, 0) and cur_t < wake_t and (today_morning - local_now) >= timedelta(hours=3):
        return today_morning.astimezone(timezone.utc)

    target_date = local_now.date() + timedelta(days=1)
    morning_local = datetime(
        target_date.year, target_date.month, target_date.day, wake_t.hour, wake_t.minute, 0, tzinfo=tz
    )
    return morning_local.astimezone(timezone.utc)


def compute_next_interval_run(
    *,
    now_utc: datetime,
    base_interval_seconds: float,
    interval_mode: str = "random",
    jitter_pct: int = DEFAULT_JITTER_PCT,
    timezone_name: str = "America/New_York",
    active_start_time: Optional[str] = "08:30",
    active_end_time: Optional[str] = "21:30",
    rng: Optional[random.Random] = None,
) -> datetime:
    norm_now = ensure_utc(now_utc) or datetime.now(timezone.utc)
    if (interval_mode or "random") == "random":
        delta_secs = apply_random_jitter(base_interval_seconds, jitter_pct=jitter_pct, rng=rng)
    else:
        delta_secs = max(MIN_INTERVAL_SECONDS, float(base_interval_seconds))
    candidate = norm_now + timedelta(seconds=delta_secs)
    return apply_active_hours(candidate, timezone_name, active_start_time, active_end_time)

