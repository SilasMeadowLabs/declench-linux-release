import math
from typing import Any, Dict, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from declench_gnome.themes import ThemePalette, get_theme, hex_to_rgb_floats  # noqa: E402


class StackedTimeseriesChart(Gtk.DrawingArea):
    """Native GTK4 Cairo stacked bar chart rendering 24h / 14d / 12m check-in buckets."""

    def __init__(self) -> None:
        super().__init__()
        self._timeseries: Optional[Dict[str, Any]] = None
        self._palette: ThemePalette = get_theme("midnight")
        self.set_content_height(185)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)

    def set_data(self, timeseries: Dict[str, Any], palette: ThemePalette) -> None:
        self._timeseries = timeseries
        self._palette = palette
        self.queue_draw()

    def _on_draw(
        self,
        _area: Gtk.DrawingArea,
        cr: Any,
        width: int,
        height: int,
    ) -> None:
        buckets = (self._timeseries or {}).get("buckets") or []
        max_total = max(1, int((self._timeseries or {}).get("max_bucket_total") or 1))
        pal = self._palette

        muted_r, muted_g, muted_b = hex_to_rgb_floats(pal.text_muted)
        c_relaxed = hex_to_rgb_floats(pal.accent_primary)
        c_reset = hex_to_rgb_floats(pal.accent_secondary)
        c_urge = hex_to_rgb_floats(pal.accent_amber)
        c_missed = hex_to_rgb_floats(pal.accent_rose)

        pad_left = 10.0
        pad_right = 10.0
        pad_top = 12.0
        pad_bottom = 28.0
        plot_w = max(40.0, float(width) - pad_left - pad_right)
        plot_h = max(40.0, float(height) - pad_top - pad_bottom)

        # Baseline axis
        cr.set_source_rgba(muted_r, muted_g, muted_b, 0.25)
        cr.set_line_width(1.0)
        cr.move_to(pad_left, pad_top + plot_h)
        cr.line_to(pad_left + plot_w, pad_top + plot_h)
        cr.stroke()

        if not buckets:
            return

        n = len(buckets)
        slot_w = plot_w / n
        bar_w = max(4.0, min(26.0, slot_w * 0.64))
        label_stride = 1 if n <= 12 else (2 if n <= 16 else 4)

        for idx, bucket in enumerate(buckets):
            cx = pad_left + (idx + 0.5) * slot_w
            bx = cx - bar_w / 2.0
            y_cursor = pad_top + plot_h

            segments = [
                (
                    int(bucket.get("already_relaxed") or 0)
                    + int(bucket.get("followed_other") or 0),
                    c_relaxed,
                ),
                (int(bucket.get("reset_now") or 0), c_reset),
                (int(bucket.get("urge_resisted") or 0), c_urge),
                (int(bucket.get("missed") or 0), c_missed),
            ]
            for count, (r, g, b) in segments:
                if count <= 0:
                    continue
                seg_h = max(3.0, (count / max_total) * plot_h)
                y_top = max(pad_top, y_cursor - seg_h)
                cr.set_source_rgb(r, g, b)
                self._rounded_rect(cr, bx, y_top, bar_w, y_cursor - y_top, radius=3.0)
                cr.fill()
                y_cursor = y_top - 1.0

            if idx % label_stride == 0 or idx == n - 1:
                lbl = str(bucket.get("short_label") or "")
                cr.set_source_rgba(muted_r, muted_g, muted_b, 0.85)
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(10.0)
                ext = cr.text_extents(lbl)
                cr.move_to(cx - ext.width / 2.0, pad_top + plot_h + 16.0)
                cr.show_text(lbl)

    @staticmethod
    def _rounded_rect(
        cr: Any, x: float, y: float, w: float, h: float, radius: float
    ) -> None:
        r = min(radius, w / 2.0, h / 2.0)
        if r <= 0.5:
            cr.rectangle(x, y, w, h)
            return
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

