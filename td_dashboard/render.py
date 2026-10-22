"""HTML fragments shared by the pages: chips, tables, charts, feeds, and action forms."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from store import ACTIVE, Activity, Bucket, Issue, PRIORITIES, STATUS_LABELS, active_sort_key


RANGES = (7, 30, 90)
DEFAULT_RANGE = 30
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]}]")
esc = html.escape


class Clock:
    def __init__(self, tz: ZoneInfo, now: datetime) -> None:
        self.tz, self.now = tz, now

    def full(self, when: datetime) -> str:
        local = when.astimezone(self.tz)
        return f"{local:%a %b} {local.day}, {local.year} at {local:%I:%M %p %Z}".replace(" 0", " ")

    def short(self, when: datetime) -> str:
        local = when.astimezone(self.tz)
        return f"{local:%b} {local.day}, {local:%I:%M %p}".replace(", 0", ", ")

    def relative(self, when: datetime | None) -> str:
        if not when:
            return '<span class="muted">—</span>'
        seconds = (self.now - when).total_seconds()
        local, today = when.astimezone(self.tz), self.now.astimezone(self.tz).date()
        if seconds < 60:
            text = "just now"
        elif seconds < 3600:
            text = f"{int(seconds // 60)}m ago"
        elif seconds < 86400 and local.date() == today:
            text = f"{int(seconds // 3600)}h ago"
        elif local.date() == today - timedelta(days=1):
            text = "yesterday"
        elif local.year == today.year:
            text = f"{local:%b} {local.day}"
        else:
            text = f"{local:%b} {local.day}, {local.year}"
        return f'<time datetime="{when.isoformat()}" title="{esc(self.full(when))}">{esc(text)}</time>'


def linkify(text: str) -> str:
    out, position = [], 0
    for match in URL_PATTERN.finditer(text):
        out.append(esc(text[position:match.start()]))
        url = match.group(0)
        out.append(f'<a href="{esc(url)}" rel="noreferrer noopener" target="_blank">{esc(url)}</a>')
        position = match.end()
    out.append(esc(text[position:]))
    return "".join(out)


def truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def status_chip(status: str) -> str:
    key = status if status in STATUS_LABELS else "open"
    return f'<span class="chip s-{key}"><span class="dot" aria-hidden="true"></span>{esc(STATUS_LABELS.get(status, status))}</span>'


def priority_text(priority: str) -> str:
    level = PRIORITIES.get(priority, 9)
    css = "p-high" if level <= 1 else "p-low" if level >= 3 else "p-mid"
    return f'<span class="{css}">{esc(priority or "—")}</span>'


def issue_href(project: str, issue_id: str) -> str:
    return f"/r/{quote(project, safe='')}/{quote(issue_id, safe='')}"


def repo_href(project: str, days: int | None = None) -> str:
    suffix = f"?{urlencode({'range': days})}" if days and days != DEFAULT_RANGE else ""
    return f"/r/{quote(project, safe='')}{suffix}"


def issue_link(issue: Issue) -> str:
    return (f'<a class="issue" href="{issue_href(issue.project, issue.id)}"><span class="id">{esc(issue.id)}</span> '
            f'{esc(issue.title)}</a>')


def nice_ceiling(value: int) -> int:
    if value <= 1:
        return 1
    magnitude = 10 ** (len(str(value)) - 1)
    for step in (1, 2, 5, 10):
        if step * magnitude >= value:
            return step * magnitude
    return 10 * magnitude


def bucket_label(bucket: Bucket) -> str:
    if bucket.start == bucket.end:
        return f"{bucket.start:%b} {bucket.start.day}"
    if bucket.start.month == bucket.end.month:
        return f"{bucket.start:%b} {bucket.start.day}–{bucket.end.day}"
    return f"{bucket.start:%b} {bucket.start.day} – {bucket.end:%b} {bucket.end.day}"


def column_chart(buckets: list[Bucket], noun: str, label: str) -> str:
    """A single-series column chart with a per-column hover and focus tooltip."""
    width, height, left, right, top, bottom = 720, 180, 44, 8, 18, 30
    plot_w, plot_h = width - left - right, height - top - bottom
    ceiling = nice_ceiling(max((b.count for b in buckets), default=0))
    band = plot_w / max(len(buckets), 1)
    bar_w = max(2.0, min(24.0, band - 2))
    base = top + plot_h
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">']
    ticks = sorted({0, ceiling} | ({ceiling // 2} if ceiling % 2 == 0 and ceiling >= 2 else set()))
    for tick in ticks:
        y = base - tick / ceiling * plot_h
        parts.append(f'<line class="{"axis" if tick == 0 else "grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>')
    peak = max(range(len(buckets)), key=lambda i: buckets[i].count, default=None)
    for i, bucket in enumerate(buckets):
        x0 = left + i * band
        x = x0 + (band - bar_w) / 2
        h = bucket.count / ceiling * plot_h
        tip_value = f"{bucket.count} {noun}"
        parts.append(f'<g class="col" tabindex="0" data-tip-value="{esc(tip_value)}" '
                     f'data-tip-label="{esc(bucket_label(bucket))}" aria-label="{esc(bucket_label(bucket))}: {esc(tip_value)}">')
        parts.append(f'<rect class="hit" x="{x0:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}"/>')
        if h > 0:
            r = min(4.0, bar_w / 2, h)
            y = base - h
            parts.append(f'<path class="bar" d="M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                         f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base:.1f} Z"/>')
        if i == peak and bucket.count:
            parts.append(f'<text class="value" x="{x + bar_w / 2:.1f}" y="{base - h - 5:.1f}" '
                         f'text-anchor="middle">{bucket.count}</text>')
        parts.append("</g>")
    for i in sorted({0, len(buckets) // 2, len(buckets) - 1}):
        if 0 <= i < len(buckets):
            anchor = "start" if i == 0 else "end" if i == len(buckets) - 1 else "middle"
            x = left + i * band + (0 if anchor == "start" else band if anchor == "end" else band / 2)
            parts.append(f'<text class="tick" x="{x:.1f}" y="{height - 6}" text-anchor="{anchor}">'
                         f'{esc(bucket_label(buckets[i]))}</text>')
    parts.append("</svg>")
    rows = "".join(f"<tr><td>{esc(bucket_label(b))}</td><td class=\"num\">{b.count}</td></tr>" for b in reversed(buckets))
    return ("".join(parts) + f'<details class="table-view"><summary>Show as table</summary><div class="scroll">'
            f'<table><thead><tr><th>Period</th><th class="num">{esc(noun.capitalize())}</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div></details>")


def sparkline(buckets: list[Bucket], ceiling: int, label: str) -> str:
    width, height = 96, 22
    band = width / max(len(buckets), 1)
    bar_w = max(1.0, band - 2)
    bars = []
    for i, bucket in enumerate(buckets):
        h = 0 if not bucket.count else max(1.5, bucket.count / max(ceiling, 1) * (height - 2))
        if h:
            bars.append(f'<rect x="{i * band + (band - bar_w) / 2:.1f}" y="{height - h:.1f}" width="{bar_w:.1f}" '
                        f'height="{h:.1f}" rx="{min(1.0, bar_w / 2):.1f}"/>')
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">'
            f'<line x1="0" x2="{width}" y1="{height - 0.5}" y2="{height - 0.5}"/>{"".join(bars)}</svg>')


def tiles(issue_counts: dict[str, int], closed_in_range: int, days: int) -> str:
    items = [(STATUS_LABELS[s], issue_counts[s], s) for s in ACTIVE]
    items.append((f"Closed in {days} days", closed_in_range, "closed"))
    return '<section class="tiles">' + "".join(
        f'<div class="tile"><div class="tile-label"><span class="chip s-{key}"><span class="dot" aria-hidden="true">'
        f'</span></span>{esc(label)}</div><div class="tile-value">{value:,}</div></div>'
        for label, value, key in items) + "</section>"


def range_filter(days: int, base: str) -> str:
    links = []
    for option in RANGES:
        href = base if option == DEFAULT_RANGE else f"{base}?{urlencode({'range': option})}"
        current = ' aria-current="true"' if option == days else ""
        links.append(f'<a href="{esc(href)}"{current}>Last {option} days</a>')
    return f'<nav class="filters" aria-label="Time range">{"".join(links)}</nav>'


def active_table(issues: list[Issue], clock: Clock, show_repo: bool) -> str:
    if not issues:
        return '<p class="empty">Nothing is open. Every task is closed.</p>'
    rows = []
    for issue in sorted(issues, key=active_sort_key):
        repo = (f'<td class="repo"><a href="{repo_href(issue.project)}">{esc(issue.project)}</a></td>'
                if show_repo else "")
        rows.append(f"<tr><td>{status_chip(issue.status)}</td>{repo}<td>{issue_link(issue)}</td>"
                    f'<td class="wide">{priority_text(issue.priority)}</td><td class="wide">{esc(issue.type)}</td>'
                    f'<td class="when">{clock.relative(issue.last_change)}</td></tr>')
    head = "<th>Repository</th>" if show_repo else ""
    return (f'<div class="scroll"><table><thead><tr><th>Status</th>{head}<th>Issue</th><th class="wide">Priority</th>'
            f'<th class="wide">Type</th><th class="when">Updated</th></tr></thead><tbody>{"".join(rows)}</tbody>'
            "</table></div>")


KIND_LABELS = {"progress": "Progress", "decision": "Decision", "blocker": "Blocker", "result": "Result",
               "security": "Security", "handoff": "Handoff", "review": "Review", "comment": "Comment",
               "status": "Status"}


def activity_feed(items: list[Activity], issues: dict[tuple[str, str], Issue], clock: Clock, show_repo: bool,
                  full: bool = False) -> str:
    if not items:
        return '<p class="empty">No activity yet.</p>'
    rows = []
    for item in items:
        issue = issues.get((item.project, item.issue_id))
        target = ("" if full else issue_link(issue) if issue else
                  f'<span class="id">{esc(item.issue_id)}</span>' if item.issue_id else "")
        repo = (f'<a class="repo" href="{repo_href(item.project)}">{esc(item.project)}</a>' if show_repo else "")
        kind = esc(KIND_LABELS.get(item.kind, item.kind.capitalize()))
        if full:
            body = f'<div class="text">{linkify(item.text)}</div>' if item.text and not item.details else ""
            for heading, entries in item.details:
                body += (f'<div class="detail"><div class="detail-label">{esc(heading)}</div><ul>'
                         + "".join(f"<li>{linkify(entry)}</li>" for entry in entries) + "</ul></div>")
        else:
            body = f'<div class="text">{esc(truncate(item.text))}</div>' if item.text else ""
        rows.append(f'<li class="event k-{esc(item.kind)}"><div class="event-head"><span class="kind">{kind}</span>'
                    f'{repo}{target}<span class="when">{clock.relative(item.when)}</span></div>{body}</li>')
    return f'<ol class="feed{"" if full else " compact"}">{"".join(rows)}</ol>'


def card(title: str, body: str, note: str = "") -> str:
    note_html = f'<span class="card-note">{note}</span>' if note else ""
    return f'<section class="card"><header class="card-head"><h2>{esc(title)}</h2>{note_html}</header>{body}</section>'
