"""Draw the card as SVG, light and dark, from a `Summary` and nothing else.

Two layouts share the pieces below: the wide card (840 wide) and a narrow phone card (400 wide,
one column, text no smaller than 15, so it stays readable when GitHub scales it to a 360px screen).
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
PHONE_WIDTH = 400
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


def _heading(x: float, y: float, label: str, theme: Theme, size: int = 13) -> str:
    return _text(x, y, label, size, theme.muted, weight=700, family=MONO, spacing=2)


def _open(summary: Summary, theme: Theme, width: int, height: int) -> List[str]:
    """The svg tag, the background and the border."""
    label = "AI usage over the last 30 days: %s tokens, %s API-equivalent" % (fmt_tokens(summary.total_tokens), fmt_cost(summary.total_cost_usd))
    return [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="%s">'
        % (width, height, width, height, escape(label)),
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
        % (theme.bg_from, theme.bg_to),
        '<rect width="%d" height="%d" rx="16" fill="url(#bg)"/>' % (width, height),
        '<rect x="0.75" y="0.75" width="%s" height="%s" rx="15.5" fill="none" stroke="%s" stroke-width="1.5"/>'
        % (width - 1.5, height - 1.5, theme.border),
    ]


def _tool_rows(summary: Summary, t: Theme, x: float, right: float, tokens_x: float, y0: float, pitch: float) -> List[str]:
    out = []
    for i, tool in enumerate(summary.tools):
        y = y0 + i * pitch
        colour = t.tools.get(tool.label, t.tools["default"])
        out.append('<circle cx="%s" cy="%s" r="5" fill="%s"/>' % (x + 6, y - 5, colour))
        out.append(_text(x + 20, y, _clip(tool.label, 16), 15, t.text))
        out.append(_text(tokens_x, y, fmt_tokens(tool.tokens), 15, t.text, anchor="end", weight=700))
        out.append(_text(right, y, fmt_cost(tool.cost_usd), 15, t.cost, anchor="end"))
    if not summary.tools:
        out.append(_text(x, y0, "No usage in this period", 15, t.muted))
    return out


def _model_rows(summary: Summary, t: Theme, x: float, right: float, tokens_x: float, y0: float, pitch: float, clip: int) -> List[str]:
    out = []
    bar_width = right - x
    for i, model in enumerate(summary.models):
        y = y0 + i * pitch
        share = model.tokens / summary.total_tokens if summary.total_tokens else 0
        out.append(_text(x, y, _clip(model.label, clip), 15, t.muted if model.label == "other" else t.text))
        out.append(_text(tokens_x, y, fmt_tokens(model.tokens), 15, t.text, anchor="end", weight=700))
        out.append(_text(right, y, fmt_cost(model.cost_usd), 15, t.cost, anchor="end"))
        out.append('<rect x="%s" y="%s" width="%s" height="3" rx="1.5" fill="%s"/>' % (x, y + 6, bar_width, t.track))
        if share > 0:
            out.append('<rect x="%s" y="%s" width="%s" height="3" rx="1.5" fill="%s"/>' % (x, y + 6, max(2.0, round(bar_width * share, 2)), t.models))
    if not summary.models:
        out.append(_text(x, y0, "No usage in this period", 15, t.muted))
    return out


def render(summary: Summary, theme: Theme) -> str:
    """The wide card, 840 x 392."""
    t = theme
    out = _open(summary, t, WIDTH, HEIGHT)
    add = out.append

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
    add(_heading(28, 156, "TOKENS PER DAY", t))
    add(_text(400, 156, "peak " + fmt_tokens(peak), 13, t.muted, anchor="end"))
    out.extend(_chart(summary, t, 28, 236, 372, 64))
    add(_text(28, 258, fmt_day(summary.first_day), 13, t.muted))
    add(_text(400, 258, fmt_day(summary.last_day), 13, t.muted, anchor="end"))
    add(_heading(28, 290, "BY TOOL", t))
    out.extend(_tool_rows(summary, t, 28, 400, 300, 314, 22))

    # Right column: top models.
    add(_heading(440, 156, "TOP MODELS", t))
    out.extend(_model_rows(summary, t, 440, 812, 722, 188, 28, 28))
    add("</svg>")
    return "\n".join(out) + "\n"


def render_phone(summary: Summary, theme: Theme) -> str:
    """The phone card: 400 wide, one column, no text under 15 (about 11px at the 294px GitHub gives it on a 360px phone). Its height follows the rows."""
    t = theme
    left, right = 20, 380
    tools_y = 346
    models_head = tools_y + max(len(summary.tools), 1) * 26 + 22
    models_y = models_head + 32
    height = models_y + max(len(summary.models), 1) * 32 - 8 + 4
    out = _open(summary, t, PHONE_WIDTH, height)
    add = out.append

    add(_text(left, 38, "AI USAGE · LAST 30 DAYS", 15, t.muted, weight=700, family=MONO, spacing=2))
    add(_text(left, 60, "updated " + fmt_date(summary.updated), 15, t.muted))
    add(_text(left, 112, fmt_tokens(summary.total_tokens), 40, t.text, weight=800))
    add(_text(left, 134, "tokens", 15, t.muted))
    add(_text(200, 112, fmt_cost(summary.total_cost_usd), 40, t.cost, weight=800))
    add(_text(200, 134, "API-equivalent", 15, t.muted))
    add('<line x1="%d" y1="150" x2="%d" y2="150" stroke="%s" stroke-width="1"/>' % (left, right, t.border))

    peak = max([d.tokens for d in summary.daily] + [0])
    add(_heading(left, 178, "TOKENS PER DAY", t, 15))
    add(_text(right, 178, "peak " + fmt_tokens(peak), 15, t.muted, anchor="end"))
    out.extend(_chart(summary, t, left, 260, right - left, 66))
    add(_text(left, 284, fmt_day(summary.first_day), 15, t.muted))
    add(_text(right, 284, fmt_day(summary.last_day), 15, t.muted, anchor="end"))

    add(_heading(left, 318, "BY TOOL", t, 15))
    out.extend(_tool_rows(summary, t, left, right, 250, tools_y, 26))

    add(_heading(left, models_head, "TOP MODELS", t, 15))
    out.extend(_model_rows(summary, t, left, right, 290, models_y, 32, 26))
    add("</svg>")
    return "\n".join(out) + "\n"
