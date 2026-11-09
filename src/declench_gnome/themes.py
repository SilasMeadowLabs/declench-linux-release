from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class ThemePalette:
    key: str
    display_name: str
    badge: str
    summary: str
    is_light: bool
    bg_primary: str
    bg_card: str
    text_primary: str
    text_muted: str
    accent_primary: str
    accent_secondary: str
    accent_amber: str
    accent_rose: str


THEMES: List[ThemePalette] = [
    ThemePalette(
        key="midnight",
        display_name="Midnight Slate",
        badge="Default · Dark",
        summary="Calm deep-slate studio dark mode with crisp emerald and sky accents.",
        is_light=False,
        bg_primary="#0f172a",
        bg_card="#1e293b",
        text_primary="#f8fafc",
        text_muted="#94a3b8",
        accent_primary="#10b981",
        accent_secondary="#38bdf8",
        accent_amber="#f59e0b",
        accent_rose="#f43f5e",
    ),
    ThemePalette(
        key="daylight",
        display_name="Daylight Clean",
        badge="Light",
        summary="Airy soft-slate daylight layout with sharp navy typography.",
        is_light=True,
        bg_primary="#f1f5f9",
        bg_card="#ffffff",
        text_primary="#0f172a",
        text_muted="#475569",
        accent_primary="#059669",
        accent_secondary="#2563eb",
        accent_amber="#d97706",
        accent_rose="#e11d48",
    ),
    ThemePalette(
        key="girl_power",
        display_name="Girl Power",
        badge="Rose Pop · Light",
        summary="Bright rose-blush cards with vibrant hot-pink accents and berry ink.",
        is_light=True,
        bg_primary="#fff1f6",
        bg_card="#ffffff",
        text_primary="#4a044e",
        text_muted="#86198f",
        accent_primary="#ec4899",
        accent_secondary="#f43f5e",
        accent_amber="#d97706",
        accent_rose="#be123c",
    ),
    ThemePalette(
        key="berry_glow",
        display_name="Berry Glow",
        badge="Dark Pink",
        summary="Velvet plum dark mode paired with luminous pink highlights.",
        is_light=False,
        bg_primary="#1a0b16",
        bg_card="#2b1325",
        text_primary="#fdf2f8",
        text_muted="#f9a8d4",
        accent_primary="#f472b6",
        accent_secondary="#c084fc",
        accent_amber="#fbbf24",
        accent_rose="#fb7185",
    ),
    ThemePalette(
        key="lavender",
        display_name="Lavender Calm",
        badge="Soft Violet · Light",
        summary="Gentle lilac surfaces with calming iris-violet controls.",
        is_light=True,
        bg_primary="#f6f3ff",
        bg_card="#ffffff",
        text_primary="#2e1065",
        text_muted="#6b21a8",
        accent_primary="#7c3aed",
        accent_secondary="#0ea5e9",
        accent_amber="#d97706",
        accent_rose="#e11d48",
    ),
    ThemePalette(
        key="matcha",
        display_name="Matcha Spa",
        badge="Botanical · Light",
        summary="Fresh sage-cream surfaces inspired by restorative botanical spas.",
        is_light=True,
        bg_primary="#f5f8f2",
        bg_card="#ffffff",
        text_primary="#14532d",
        text_muted="#3f6212",
        accent_primary="#15803d",
        accent_secondary="#0d9488",
        accent_amber="#ca8a04",
        accent_rose="#be123c",
    ),
    ThemePalette(
        key="sunset",
        display_name="Sunset Peach",
        badge="Warm Coral · Light",
        summary="Warm apricot-cream cards with energizing coral-amber highlights.",
        is_light=True,
        bg_primary="#fff7ed",
        bg_card="#ffffff",
        text_primary="#431407",
        text_muted="#9a3412",
        accent_primary="#ea580c",
        accent_secondary="#f59e0b",
        accent_amber="#d97706",
        accent_rose="#dc2626",
    ),
    ThemePalette(
        key="obsidian",
        display_name="Obsidian OLED",
        badge="Pure Black · Dark",
        summary="True zero-glare black surfaces crafted for high-contrast monitors.",
        is_light=False,
        bg_primary="#000000",
        bg_card="#0b0f16",
        text_primary="#f8fafc",
        text_muted="#94a3b8",
        accent_primary="#10b981",
        accent_secondary="#06b6d4",
        accent_amber="#f59e0b",
        accent_rose="#f43f5e",
    ),
]

THEME_MAP: Dict[str, ThemePalette] = {t.key: t for t in THEMES}


def get_theme(key: str | None) -> ThemePalette:
    cleaned = (key or "").strip().lower()
    return THEME_MAP.get(cleaned, THEME_MAP["midnight"])


def hex_to_rgb_floats(hex_color: str) -> Tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0.06, 0.72, 0.50)
    return (
        int(h[0:2], 16) / 255.0,
        int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0,
    )


def build_gtk_css(palette: ThemePalette) -> str:
    border_alpha = "rgba(15, 23, 42, 0.10)" if palette.is_light else "rgba(255, 255, 255, 0.09)"
    btn_fg = "#ffffff" if palette.is_light else "#04130d"
    return f"""
    window.declench-window {{
        background-color: {palette.bg_primary};
        color: {palette.text_primary};
    }}
    .declench-card {{
        background-color: {palette.bg_card};
        color: {palette.text_primary};
        border-radius: 16px;
        border: 1px solid {border_alpha};
        padding: 16px;
    }}
    .declench-pro-banner {{
        background-color: {palette.bg_card};
        border-radius: 14px;
        border: 1px solid {palette.accent_secondary};
        padding: 14px;
    }}
    .declench-badge {{
        color: {palette.accent_primary};
        font-weight: 700;
        font-size: 0.78em;
        letter-spacing: 0.04em;
    }}
    .declench-muted {{
        color: {palette.text_muted};
    }}
    .declench-metric-val {{
        color: {palette.accent_primary};
        font-size: 1.65em;
        font-weight: 800;
    }}
    .declench-metric-val-cyan {{
        color: {palette.accent_secondary};
        font-size: 1.65em;
        font-weight: 800;
    }}
    .declench-metric-val-amber {{
        color: {palette.accent_amber};
        font-size: 1.65em;
        font-weight: 800;
    }}
    button.declench-primary-cta {{
        background-image: none;
        background-color: {palette.accent_primary};
        color: {btn_fg};
        font-weight: 700;
        border-radius: 12px;
        padding: 8px 16px;
    }}
    .declench-method-img {{
        border-radius: 12px;
        border: 1px solid {border_alpha};
    }}
    """

