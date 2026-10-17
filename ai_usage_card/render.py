"""Draw the card as SVG, light and dark, from a `Summary` and nothing else.

Colours come from the profile's `assets/banner.svg` (dark navy panel, amber, coral, violet,
teal and green accents). The caller runs `privacy.check_privacy` on the output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List
from xml.sax.saxutils import escape

from .summarise import Summary

WIDTH, HEIGHT = 840, 392
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SANS = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
MONO = "'Courier New', ui-monospace, monospace"


@dataclass(frozen=True)
class Theme:
    name: str
    bg_from: str
    bg_to: str
    border: str
    text: str
    muted: str
    track: str
    bars: str
    cost: str
    models: str
    tools: dict


DARK = Theme("dark", "#0D1420", "#141C29", "#2E3D4B", "#FFFFFF", "#94A5B4", "#26323F", "#F59E0B", "#FFD166", "#9C82E2",
             {"Claude Code": "#E4715C", "Codex": "#4FC3D9", "Grok": "#9C82E2", "Cursor": "#4EB586", "default": "#E8A33D"})
LIGHT = Theme("light", "#FFFFFF", "#F6F8FA", "#D0D7DE", "#1F2328", "#59636E", "#E3E8ED", "#D97706", "#B45309", "#7C5CD6",
              {"Claude Code": "#CF4F37", "Codex": "#0E8FAB", "Grok": "#7C5CD6", "Cursor": "#1F8A5B", "default": "#B7791F"})

def fmt_tokens(tokens: int) -> str:
    """1.2B, 12.3M, 123M, 4.5K; a real zero stays "0"."""
    if tokens < 1000:
        return str(int(tokens))
    for power, suffix in enumerate("KMBT", start=1):
        scaled = tokens / 1000.0 ** power
        digits = 1 if round(scaled, 1) < 100 else 0
        if round(scaled, digits) < 1000 or suffix == "T":  # 999.96K must not print as 1000.0K
            return "%.*f%s" % (digits, scaled, suffix)
    raise AssertionError("unreachable")


def fmt_cost(cost: float) -> str:
    if cost <= 0:
        return "$0"
    if round(cost, 2) < 100:  # 99.996 must not print as $100.00
        return "$%.2f" % cost
    return "${:,}".format(int(round(cost)))


def fmt_day(day: date) -> str:
    return "%d %s" % (day.day, MONTHS[day.month - 1])


def fmt_date(day: date) -> str:
    return "%d %s %d" % (day.day, MONTHS[day.month - 1], day.year)


def _clip(label: str, limit: int) -> str:
    return label if len(label) <= limit else label[:limit - 1] + "…"


def _text(x: float, y: float, content: str, size: int, fill: str, anchor: str = "start", weight: int = 400,
          family: str = SANS, spacing: float = 0) -> str:
    extra = ' letter-spacing="%s"' % spacing if spacing else ""
    return ('<text x="%s" y="%s" font-family="%s" font-size="%d" font-weight="%d" fill="%s" text-anchor="%s"%s>%s</text>'
            % (x, y, family, size, weight, fill, anchor, extra, escape(content)))


def _chart(summary: Summary, theme: Theme, x0: float, baseline: float, width: float, height: float) -> List[str]:
    parts = []
    peak = max([d.tokens for d in summary.daily] + [0])
    slot = width / len(summary.daily)
    bar = slot * 0.76
    for i, day in enumerate(summary.daily):
        x = round(x0 + i * slot + (slot - bar) / 2, 2)
        if day.tokens > 0:
            h = max(2.0, round(day.tokens / peak * height, 2))
            parts.append('<rect x="%s" y="%s" width="%s" height="%s" rx="2" fill="%s"><title>%s: %s, %s</title></rect>'
                         % (x, round(baseline - h, 2), round(bar, 2), h, theme.bars, fmt_day(day.day),
                            fmt_tokens(day.tokens), fmt_cost(day.cost_usd)))
        else:  # a real zero is drawn as a thin stub, never left blank
            parts.append('<rect x="%s" y="%s" width="%s" height="1.5" fill="%s"/>' % (x, baseline - 1.5, round(bar, 2), theme.muted))
    parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1"/>' % (x0, baseline + 0.5, x0 + width, baseline + 0.5, theme.border))
    return parts


def render(summary: Summary, theme: Theme) -> str:
    t = theme
    out = []
    add = out.append
    add('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="%s">'
        % (WIDTH, HEIGHT, WIDTH, HEIGHT, escape("AI usage over the last 30 days: %s tokens, %s API-equivalent" %
                                               (fmt_tokens(summary.total_tokens), fmt_cost(summary.total_cost_usd)))))
    add('<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
        % (t.bg_from, t.bg_to))
    add('<rect width="%d" height="%d" rx="16" fill="url(#bg)"/>' % (WIDTH, HEIGHT))
    add('<rect x="0.75" y="0.75" width="%s" height="%s" rx="15.5" fill="none" stroke="%s" stroke-width="1.5"/>' % (WIDTH - 1.5, HEIGHT - 1.5, t.border))

    # Header: label, updated date, the two headline numbers.
    add(_text(28, 38, "AI USAGE · LAST 30 DAYS", 14, t.muted, weight=700, family=MONO, spacing=3))
    add(_text(812, 38, "updated " + fmt_date(summary.updated), 14, t.muted, anchor="end"))
    add(_text(28, 88, fmt_tokens(summary.total_tokens), 46, t.text, weight=800))
    add(_text(28, 112, "tokens", 15, t.muted))
    add(_text(300, 88, fmt_cost(summary.total_cost_usd), 46, t.cost, weight=800))
    add(_text(300, 112, "API-equivalent", 15, t.muted))
    add('<line x1="28" y1="128" x2="812" y2="128" stroke="%s" stroke-width="1"/>' % t.border)

    # Left column: daily tokens, then the split by tool.
    peak = max([d.tokens for d in summary.daily] + [0])
    add(_text(28, 156, "TOKENS PER DAY", 13, t.muted, weight=700, family=MONO, spacing=2))
    add(_text(400, 156, "peak " + fmt_tokens(peak), 13, t.muted, anchor="end"))
    out.extend(_chart(summary, t, 28, 236, 372, 64))
    add(_text(28, 258, fmt_day(summary.first_day), 13, t.muted))
    add(_text(400, 258, fmt_day(summary.last_day), 13, t.muted, anchor="end"))
    add(_text(28, 290, "BY TOOL", 13, t.muted, weight=700, family=MONO, spacing=2))
    for i, tool in enumerate(summary.tools):
        y = 314 + i * 22
        colour = t.tools.get(tool.label, t.tools["default"])
        add('<circle cx="34" cy="%s" r="5" fill="%s"/>' % (y - 5, colour))
        add(_text(48, y, _clip(tool.label, 16), 15, t.text))
        add(_text(300, y, fmt_tokens(tool.tokens), 15, t.text, anchor="end", weight=700))
        add(_text(400, y, fmt_cost(tool.cost_usd), 15, t.cost, anchor="end"))
    if not summary.tools:
        add(_text(28, 314, "No usage in this period", 15, t.muted))

    # Right column: top models.
    add(_text(440, 156, "TOP MODELS", 13, t.muted, weight=700, family=MONO, spacing=2))
    for i, model in enumerate(summary.models):
        y = 188 + i * 28
        share = model.tokens / summary.total_tokens if summary.total_tokens else 0
        add(_text(440, y, _clip(model.label, 28), 15, t.muted if model.label == "other" else t.text))
        add(_text(722, y, fmt_tokens(model.tokens), 15, t.text, anchor="end", weight=700))
        add(_text(812, y, fmt_cost(model.cost_usd), 15, t.cost, anchor="end"))
        add('<rect x="440" y="%s" width="372" height="3" rx="1.5" fill="%s"/>' % (y + 6, t.track))
        if share > 0:
            add('<rect x="440" y="%s" width="%s" height="3" rx="1.5" fill="%s"/>' % (y + 6, max(2.0, round(372 * share, 2)), t.models))
    if not summary.models:
        add(_text(440, 188, "No usage in this period", 15, t.muted))
    add("</svg>")
    return "\n".join(out) + "\n"
