from dataclasses import asdict, dataclass
import random
from typing import Any, Dict, Optional

from declench_gnome.schedule_math import format_duration_seconds


DEFAULT_COACH_BUTTON_LABEL = "Check In: Jaw in 'N' Position?"
DEFAULT_EXERCISE_EVERY_N = 5


@dataclass
class CoachingPrompt:
    title: str
    message: str
    button_label: str
    category: str
    stage_badge: str
    coach_headline: str
    coach_note: str
    goal_progress_pct: int
    goal_milestone: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LocalCoachingEngine:
    """Deterministic on-device coaching and jaw-stretch prompt generator for GNOME Linux."""

    @staticmethod
    def compute_goal_progress(
        stats: Dict[str, Any],
        current_interval_seconds: Optional[float] = None,
        start_interval_seconds: Optional[float] = None,
        target_interval_seconds: Optional[float] = None,
        progressive_enabled: bool = True,
    ) -> tuple[int, str]:
        total_checkins = int(stats.get("total_checkins") or 0)
        streak = int(stats.get("current_streak") or 0)
        auto_pct = float(stats.get("automaticity_rate_pct") or 0.0)
        urges = int(stats.get("urges_resisted_count") or 0)

        volume_pts = min(35.0, total_checkins * 1.4 + min(5.0, urges * 1.0))

        interval_pts = 0.0
        if (
            progressive_enabled
            and start_interval_seconds
            and target_interval_seconds
            and current_interval_seconds
            and abs(start_interval_seconds - target_interval_seconds) > 1.0
        ):
            span = abs(start_interval_seconds - target_interval_seconds)
            if start_interval_seconds > target_interval_seconds:
                covered = max(0.0, start_interval_seconds - current_interval_seconds)
            else:
                covered = max(0.0, current_interval_seconds - start_interval_seconds)
            ratio = min(1.0, covered / span)
            if auto_pct >= 70.0 and total_checkins >= 6:
                ratio = max(ratio, 0.85)
            interval_pts = 25.0 * ratio
        elif total_checkins > 0:
            interval_pts = min(25.0, total_checkins * 2.5)

        auto_pts = min(40.0, (auto_pct * 0.36) + min(12.0, streak * 1.5))

        raw_pct = int(round(volume_pts + interval_pts + auto_pts))
        if total_checkins == 0:
            pct = 5
        elif total_checkins >= 20 and auto_pct >= 80.0 and streak >= 5:
            pct = 100
        else:
            pct = max(8, min(99, raw_pct))

        if pct >= 90:
            milestone = "Final Stretch: 'N' Posture Becoming Effortless Default"
        elif pct >= 65:
            milestone = "Strong Myelination: Unconscious 'N' Baseline Taking Hold"
        elif pct >= 35:
            milestone = "Building Momentum: Rerouting Tension Reflexes"
        elif total_checkins > 0:
            milestone = "Early Wiring: Catching and Releasing Hidden Clenches"
        else:
            milestone = "Ready to Launch: First 'N' Posture Check Queued"

        return pct, milestone

    @classmethod
    def generate_prompt(
        cls,
        *,
        stats: Optional[Dict[str, Any]] = None,
        last_outcome: Optional[str] = None,
        current_interval_seconds: Optional[float] = None,
        start_interval_seconds: Optional[float] = None,
        target_interval_seconds: Optional[float] = None,
        progressive_enabled: bool = True,
        adaptive_feedback: bool = True,
        interval_mode: str = "random",
        jitter_pct: int = 25,
        run_count: int = 0,
        exercises_enabled: bool = True,
        exercise_every_n: int = DEFAULT_EXERCISE_EVERY_N,
        force_exercise: bool = False,
        rng: Optional[random.Random] = None,
    ) -> CoachingPrompt:
        st = stats or {}
        total_checkins = int(st.get("total_checkins") or 0)
        streak = int(st.get("current_streak") or 0)
        auto_pct_val = st.get("automaticity_rate_pct")
        auto_pct = float(auto_pct_val) if auto_pct_val is not None else 0.0
        auto_disp = f"{int(round(auto_pct))}%" if auto_pct_val is not None else "0%"

        cur_fmt = format_duration_seconds(current_interval_seconds) or "20m"
        tgt_fmt = format_duration_seconds(target_interval_seconds) or "90m"
        is_random = (interval_mode or "random") == "random"
        cadence_tag = f"~{cur_fmt} random" if is_random else f"every {cur_fmt}"

        goal_pct, milestone = cls.compute_goal_progress(
            st,
            current_interval_seconds=current_interval_seconds,
            start_interval_seconds=start_interval_seconds,
            target_interval_seconds=target_interval_seconds,
            progressive_enabled=progressive_enabled,
        )

        selector = max(0, int(run_count) + total_checkins)
        every_n = max(2, int(exercise_every_n or DEFAULT_EXERCISE_EVERY_N))
        chooser = rng if rng is not None else random
        periodic_exercise = (
            exercises_enabled
            and run_count > 0
            and (run_count % every_n == 0 or chooser.random() < 0.18)
        )

        if force_exercise or periodic_exercise:
            exercise_series = [
                (
                    "Time for a Quick Jaw Stretch",
                    "Keep your tongue lightly on the roof of your mouth behind your upper front teeth, open comfortably for a slow breath, then settle back into 'N'.",
                ),
                (
                    "Quick Jaw Stretch & Cheek Massage",
                    "Take a few seconds to unclench: rub your cheeks in slow, gentle circles with two fingertips, drop your shoulders, and let your teeth float apart in 'N'.",
                ),
                (
                    "Time for a Quick Temple Release",
                    "Pause for a quick reset: lightly massage your temples and jawline for a breath or two, exhale slowly, and park your tongue back on 'N'.",
                ),
                (
                    "Quick Jaw Unwind: Stretch & Reset",
                    "Give tight jaw muscles a quick breather. Gently stretch open within an easy, pain-free range, soften your cheeks, and rest back in 'N'.",
                ),
            ]
            ex_title, ex_msg = exercise_series[selector % len(exercise_series)]
            return CoachingPrompt(
                title=ex_title,
                message=ex_msg,
                button_label=DEFAULT_COACH_BUTTON_LABEL,
                category="jaw_exercise",
                stage_badge="Occasional Jaw Stretch · Desk Reset",
                coach_headline="Time for a quick jaw stretch and gentle cheek release.",
                coach_note="Blended naturally into your regular notifications with zero extra alert spam.",
                goal_progress_pct=goal_pct,
                goal_milestone=milestone,
            )

        if total_checkins == 0:
            series = [
                (
                    "Welcome Check: Find Your 'N' Spot",
                    "Say 'N' silently and leave the tip of your tongue resting right behind your upper front teeth. Lips together, teeth apart, jaw weightless.",
                ),
                (
                    "First Scan: Drop Masseter Tension",
                    "Quick 2-second scan: separate your upper and lower teeth, park your tongue lightly on 'N', and let your shoulders drop.",
                ),
            ]
            t, m = series[selector % len(series)]
            return CoachingPrompt(
                title=t,
                message=m,
                button_label=DEFAULT_COACH_BUTTON_LABEL,
                category="kickoff",
                stage_badge="Kickoff · Local Linux Coach",
                coach_headline="Your adaptive 'N'-posture schedule is active on this Linux PC.",
                coach_note=(
                    f"Polling {cadence_tag} (±{jitter_pct}% anti-habituation variance) and stepping toward {tgt_fmt}."
                    if is_random and progressive_enabled
                    else f"Polling {cadence_tag}."
                ),
                goal_progress_pct=goal_pct,
                goal_milestone=milestone,
            )

        if last_outcome == "reset_now":
            series = [
                (
                    "Focus Scan: Check Your Jaw",
                    "Deep coding and terminal focus often trigger unconscious clenching. Separate your teeth 2 to 3 mm and park your tongue on 'N'.",
                ),
                (
                    "Posture Check: Release & Float",
                    "Notice if your molars are touching. Exhale slowly, soften your jawline, and return to passive 'N' resting posture.",
                ),
            ]
            t, m = series[selector % len(series)]
            return CoachingPrompt(
                title=t,
                message=m,
                button_label=DEFAULT_COACH_BUTTON_LABEL,
                category="tension_caught",
                stage_badge="Synapse Rerouted · Catching Tension",
                coach_headline="Catching tension in the act is how neuroplasticity rewires habits.",
                coach_note=f"Notice + release trains your new baseline. Current cadence: {cadence_tag}.",
                goal_progress_pct=goal_pct,
                goal_milestone=milestone,
            )

        if auto_pct >= 65.0 and streak >= 3:
            series = [
                (
                    f"Effortless 'N' Check ({auto_disp} Automatic)",
                    "Quick check: still resting naturally in 'N' with lips together and teeth apart? Tap Already in 'N' to widen your interval.",
                ),
                (
                    f"{streak}-Check Streak: Confirm Relaxed Jaw",
                    "Your relaxed resting baseline is sticking. Confirm freeway space between your teeth and drop shoulder tension.",
                ),
            ]
            t, m = series[selector % len(series)]
            return CoachingPrompt(
                title=t,
                message=m,
                button_label=DEFAULT_COACH_BUTTON_LABEL,
                category="high_automaticity",
                stage_badge=f"Automaticity {auto_disp} · Streak {streak}",
                coach_headline="Relaxed 'N' posture is becoming your effortless default.",
                coach_note=f"Intervals widen automatically toward {tgt_fmt} as automaticity climbs.",
                goal_progress_pct=goal_pct,
                goal_milestone=milestone,
            )

        series = [
            (
                "Declench Posture Check",
                "Lips gently together, teeth slightly apart, tongue resting lightly on the 'N' spot behind your upper front teeth.",
            ),
            (
                "2-Second Jaw & Shoulder Scan",
                "Unclench your jaw, let a small gap float between your teeth, and soften your temples while you work.",
            ),
            (
                "Mindful 'N' Posture Check",
                "Check your bite right now: are your teeth separated and your tongue resting calmly on the palate?",
            ),
        ]
        t, m = series[selector % len(series)]
        return CoachingPrompt(
            title=t,
            message=m,
            button_label=DEFAULT_COACH_BUTTON_LABEL,
            category="steady",
            stage_badge=f"Streak {streak} · Automaticity {auto_disp}",
            coach_headline="Consistent daytime micro-checks build lasting jaw relief.",
            coach_note=f"Current schedule: {cadence_tag}.",
            goal_progress_pct=goal_pct,
            goal_milestone=milestone,
        )

