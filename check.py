#!/usr/bin/env python3
"""Daily Airbrake check: new groups, reappearing groups and spikes for the configured projects.

Usage: python3 check.py [--env FILE] [--date YYYY-MM-DD] [--days N]
       python3 check.py [--env FILE] --serve [PORT]
  --env    config file to read (default: .env next to this script). See .env.example.
  --date   the day the check is run for (default: today). Window = the N full UTC days before it.
  --days   window length (default: every day since the last report's date, so nothing is skipped when a day
           is missed; with no earlier report: 1, or 3 = Fri+Sat+Sun when --date is a Monday).
  --serve  serve reports/ on http://127.0.0.1:PORT (default AIRBRAKE_PORT or 8787) so the "Reviewed"
           checkboxes save to reviewed.json.
Writes reports/<date>.html, .md and .json (snapshot used by the next run), prints the markdown.

Reviewed groups are hidden from later reports unless their daily avg reaches SPIKE_RATIO x the rate they had
when reviewed. Each report also marks which items were not in the previous report.
"""
import argparse
import collections
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """Read KEY=value lines from .env (or the --env FILE) into os.environ. Variables already set in the shell win."""
    path = sys.argv[sys.argv.index("--env") + 1] if "--env" in sys.argv[:-1] else os.path.join(HERE, ".env")
    if not os.path.exists(path):
        if "--env" in sys.argv:
            sys.exit(f"Config file not found: {path}")
        return
    for line in open(path):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (s.strip() for s in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        os.environ.setdefault(name, value)


def env_list(name, default=""):
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


def parse_projects():
    """AIRBRAKE_PROJECTS=id[:name],... -> {id: name or None}; missing names are fetched from Airbrake."""
    projects = {}
    for entry in env_list("AIRBRAKE_PROJECTS"):
        pid, _, name = entry.partition(":")
        projects[int(pid)] = name.strip() or None
    return projects


def parse_breakdown():
    """AIRBRAKE_BREAKDOWN=id:param[:label],... -> {id: (param, label)}."""
    out = {}
    for entry in env_list("AIRBRAKE_BREAKDOWN"):
        pid, param, label = (entry.split(":") + [""])[:3]
        out[int(pid)] = (param.strip(), label.strip() or param.strip())
    return out


def read_key():
    if os.environ.get("AIRBRAKE_KEY"):
        return os.environ["AIRBRAKE_KEY"].strip()
    path = os.path.expanduser(os.environ.get("AIRBRAKE_KEY_FILE", "~/.airbrake_key"))
    return open(path).read().strip() if os.path.exists(path) else ""


load_env()
PROJECTS = parse_projects()
# Per project: count distinct values of this notice param (e.g. a device id) to tell one looping client from
# a widespread error.
BREAKDOWN = parse_breakdown()
BASELINE_DAYS = int(os.environ.get("BASELINE_DAYS", 30))
TREND_DAYS = int(os.environ.get("TREND_DAYS", 14))
SPIKE_RATIO = float(os.environ.get("SPIKE_RATIO", 3))            # window daily avg vs baseline daily avg
SPIKE_MIN_NOTICES = int(os.environ.get("SPIKE_MIN_NOTICES", 20))  # ignore spikes smaller than this in the window
# Only these Airbrake environments (context.environment) count.
PROD_ENVS = tuple(env_list("AIRBRAKE_ENVIRONMENTS", "production,prod"))
# Known/accepted errors, matched against "<type>: <message>"; listed collapsed under "Known / ignored".
IGNORE = re.compile(os.environ["AIRBRAKE_IGNORE"]) if os.environ.get("AIRBRAKE_IGNORE") else None
PORT = int(os.environ.get("AIRBRAKE_PORT", 8787))
API = "https://api.airbrake.io/api"
KEY = read_key()
DATA = os.path.expanduser(os.environ.get("AIRBRAKE_DATA_DIR") or HERE)
REPORTS = os.path.join(DATA, "reports")
REVIEWED = os.path.join(DATA, "reviewed.json")

KINDS = [  # key, label, short badge, needs attention
    ("new", "New groups", "New", True),
    ("reappeared", f"Reappeared after {BASELINE_DAYS}+ quiet days", "Reappeared", True),
    ("spike", "Spikes", "Spike", True),
    ("respiked", "Reviewed earlier, spiking again", "Re-spike", True),
    ("elevated", f"Elevated vs {BASELINE_DAYS}-day baseline, already high last week", "Elevated", False),
    ("recent", f"Created in the last {BASELINE_DAYS} days, still firing", "Recent", False),
    ("reviewed", "Reviewed — hidden unless they spike again", "Reviewed", False),
    ("ignored", "Known / ignored", "Ignored", False),
]


def load_reviewed():
    return json.load(open(REVIEWED)) if os.path.exists(REVIEWED) else {}


def previous_report(date):
    """Items of the latest earlier report: from its .json snapshot, or group links in its .md for older reports."""
    dates = sorted({f[:10] for f in os.listdir(REPORTS) if re.fullmatch(r"\d{4}-\d\d-\d\d\.(json|md)", f)} - {date})
    dates = [d for d in dates if d < date]
    if not dates:
        return None
    path = os.path.join(REPORTS, dates[-1])
    if os.path.exists(path + ".json"):
        items = json.load(open(path + ".json"))["items"]
        return {"date": dates[-1], "sigs": {i["sig"] for i in items},
                "ids": {f"{i['pid']}:{g}" for i in items for g in i["ids"]}}
    links = re.findall(r"airbrake\.io/projects/(\d+)/groups/(\d+)", open(path + ".md").read())
    return {"date": dates[-1], "sigs": set(), "ids": {f"{p}:{g}" for p, g in links}}


def get(path, **params):
    url = f"{API}/{path}?" + urllib.parse.urlencode({**params, "key": KEY})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        raise RuntimeError(f"GET {path} failed: {type(e).__name__} {getattr(e, 'code', '')}".strip()) from None


def daily(stats):
    return dict(zip((t[:10] for t in stats["time"]), stats["accepted"]))


def active_groups(pid, since):
    page = 1
    while True:
        groups = get(f"v4/projects/{pid}/groups", limit=100, order="last_notice", page=page)["groups"]
        for g in groups:
            if g["lastNoticeAt"] < since:
                return
            yield g
        if len(groups) < 100:
            return
        page += 1


def breakdown(pid, gid, win_days, max_pages=10):
    """Distinct values of the project's breakdown param among the group's notices in the window
    (e.g. one looping device vs fleet-wide). Falls back to context.hostname when the param is missing."""
    param, label = BREAKDOWN[pid]
    seen = collections.Counter()
    for page in range(1, max_pages + 1):
        notices = get(f"v4/projects/{pid}/groups/{gid}/notices", limit=100, page=page)["notices"]
        for n in notices:
            if n["createdAt"][:10] in win_days:
                seen[(n.get("params") or {}).get(param) or n.get("context", {}).get("hostname") or "?"] += 1
        if len(notices) < 100 or notices[-1]["createdAt"][:10] < win_days[0]:
            break
    return {"label": label, "sampled": sum(seen.values()), "distinct": len(seen),
            "top": seen.most_common(3)} if seen else None


def check_project(pid, win_days, base_days, reviewed):
    win_start, base_start = win_days[0], base_days[0]
    all_days = base_days + win_days
    proj = collections.Counter()
    for env in PROD_ENVS:
        proj.update(daily(get(f"v5/projects/{pid}/stats", period="day", environment=env,
                              time__gte=f"{base_start}T00:00:00Z")))
    groups = [g for g in active_groups(pid, f"{win_start}T00:00:00Z")
              if (g.get("context") or {}).get("environment") in PROD_ENVS]

    def with_stats(g):
        return g, daily(get(f"v5/projects/{pid}/groups/{g['id']}/stats", period="day",
                            time__gte=f"{base_start}T00:00:00Z"))

    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(with_stats, groups))

    findings = {k: [] for k, *_ in KINDS}
    findings["other"] = []
    for g, s in rows:
        win = sum(s.get(d, 0) for d in win_days)
        if not win:
            continue
        created = g["createdAt"][:10]
        lived = [d for d in base_days if d >= created]
        base = sum(s.get(d, 0) for d in lived)
        base_avg = base / len(lived) if lived else 0
        win_avg = win / len(win_days)
        trend = [s.get(d, 0) for d in all_days[-TREND_DAYS:]]
        item = {"g": g, "win": win, "base_avg": base_avg, "win_avg": win_avg, "created": created, "trend": trend,
                "sig": sig(pid, g)}
        etype, msg, _ = parts(g)
        if IGNORE and IGNORE.search(f"{etype}: {msg}"):
            findings["ignored"].append(item)
        elif created >= win_start:
            findings["new"].append(item)
        elif base == 0:
            findings["reappeared"].append(item)
        elif win >= SPIKE_MIN_NOTICES and win_avg >= SPIKE_RATIO * base_avg:
            week = [s.get(d, 0) for d in base_days[-7:] if d >= created]
            fresh = not week or win_avg >= SPIKE_RATIO * (sum(week) / len(week))
            findings["spike" if fresh else "elevated"].append(item)
        elif created >= base_start:
            findings["recent"].append(item)
        else:
            findings["other"].append(item)
    for k, v in findings.items():
        findings[k] = merge_duplicates(v)

    # Reviewed groups stay hidden unless they now run at SPIKE_RATIO x the rate they had when reviewed.
    for k in [k for k in findings if k not in ("ignored", "respiked", "reviewed")]:
        keep = []
        for i in findings[k]:
            rev = reviewed.get(i["sig"])
            if not rev:
                keep.append(i)
                continue
            i["rev"] = rev
            ref = max(rev["win_avg"], rev["base_avg"], 1)
            again = i["win"] >= SPIKE_MIN_NOTICES and i["win_avg"] >= SPIKE_RATIO * ref
            findings["respiked" if again else "reviewed"].append(i)
        findings[k] = keep
    findings.pop("other")

    if pid in BREAKDOWN:
        flagged = [i for k in ("new", "reappeared", "spike", "respiked", "elevated") for i in findings[k]]
        with ThreadPoolExecutor(4) as ex:
            for i, b in zip(flagged, ex.map(lambda i: breakdown(pid, i["g"]["id"], win_days), flagged)):
                i["breakdown"] = b

    proj_win = sum(proj.get(d, 0) for d in win_days)
    proj_base_avg = sum(proj.get(d, 0) for d in base_days) / len(base_days)
    return {"pid": pid, "win": proj_win, "win_avg": proj_win / len(win_days), "base_avg": proj_base_avg,
            "series": [(d, proj.get(d, 0)) for d in all_days],
            "active": sum(1 for _, s in rows if any(s.get(d) for d in win_days)), **findings}


def merge_duplicates(items):
    """Airbrake sometimes splits one error into many groups (e.g. per-URL); show them as one line."""
    merged = {}
    for i in items:
        m = merged.setdefault(parts(i["g"]), {**i, "win": 0, "win_avg": 0, "base_avg": 0,
                                              "trend": [0] * len(i["trend"]), "ids": []})
        m["ids"].append(i["g"]["id"])
        for f in ("win", "win_avg", "base_avg"):
            m[f] += i[f]
        m["trend"] = [a + b for a, b in zip(m["trend"], i["trend"])]
        m["created"] = min(m["created"], i["created"])
    return sorted(merged.values(), key=lambda i: -i["win"])


def parts(g):
    e = (g.get("errors") or [{}])[0]
    msg = " ".join(str(e.get("message", "")).split())[:240]
    bt = (e.get("backtrace") or [{}])[0]
    where = f"{bt.get('file', '').replace('/PROJECT_ROOT/', '')}:{bt.get('line', '')}" if bt.get("file") else ""
    return e.get("type") or "Error", msg, where


def sig(pid, g):
    """Stable key for an error across reports; matches how merge_duplicates groups split groups."""
    return hashlib.sha1("|".join((str(pid),) + parts(g)).encode()).hexdigest()[:12]


def is_new(i, prev):
    return prev is not None and not i["seen"]


def rev_note(i):
    rev = i.get("rev")
    if not rev:
        return ""
    ref = max(rev["win_avg"], rev["base_avg"])
    return f"Reviewed {rev['reviewed_at'][:10]} at {ref:,.0f}/day — now {i['win_avg']:,.0f}/day"


def link(pid, i):
    return f"https://airbrake.io/projects/{pid}/groups/{i['g']['id']}"


def scope(b):
    if b["distinct"] == 1:
        return f"Single {b['label']}"
    if b["top"][0][1] >= 0.8 * b["sampled"]:
        return f"Mostly one {b['label']}"
    return f"Spread across {b['distinct']} {b['label']}s"


def breakdown_text(b):
    if not b:
        return ""
    top = ", ".join(f"{v} ×{c}" for v, c in b["top"])
    return f"{scope(b)}: {top} ({b['sampled']} notices sampled)"


def render_md(date, win_days, results, prev):
    hidden = sum(len(r["reviewed"]) for r in results if "error" not in r)
    out = [f"# Airbrake daily check — {date}",
           "",
           f"Window: {win_days[0]} → {win_days[-1]} (UTC, {len(win_days)} day{'s' * (len(win_days) > 1)}), "
           f"environment {'/'.join(PROD_ENVS)} only. "
           f"Baseline: previous {BASELINE_DAYS} days. Spike = window daily avg ≥ {SPIKE_RATIO:g}× baseline avg "
           f"and ≥ {SPIKE_MIN_NOTICES} notices. "
           + (f"Compared with the {prev['date']} report. " if prev else "")
           + f"{hidden} reviewed group{'s' * (hidden != 1)} hidden (no new spike).",
           "",
           "| Project | Notices in window | Daily avg vs baseline | Active groups | New | Reappeared | Spikes | Elevated |",
           "|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            out.append(f"| {PROJECTS[r['pid']]} | error: {r['error']} | | | | | | |")
            continue
        ratio = f"{r['win_avg'] / r['base_avg']:.1f}×" if r["base_avg"] else "n/a"
        out.append(f"| {PROJECTS[r['pid']]} | {r['win']} | {r['win_avg']:.0f} vs {r['base_avg']:.0f} ({ratio}) "
                   f"| {r['active']} | {len(r['new'])} | {len(r['reappeared'])} | {len(r['spike'])} | {len(r['elevated'])} |")

    shown = [k for k, *_ in KINDS if k not in ("reviewed", "ignored")]
    for r in results:
        if "error" in r or not any(r[k] for k in shown):
            continue
        out += ["", f"## {PROJECTS[r['pid']]}"]
        for k, label, _, _ in KINDS:
            if not r[k] or k not in shown:
                continue
            out += ["", f"### {label}"]
            for i in r[k]:
                g = i["g"]
                etype, msg, where = parts(g)
                flags = "".join([" [NEW since last report]" if is_new(i, prev) else "",
                                 " [muted]" if g.get("muted") else "", " [resolved]" if g.get("resolved") else "",
                                 f" [×{len(i['ids'])} groups]" if len(i["ids"]) > 1 else ""])
                trend = f", last 7d: {' '.join(map(str, i['trend'][-7:]))}" if k != "recent" else ""
                spread = f"  \n  {breakdown_text(i['breakdown'])}" if i.get("breakdown") else ""
                note = f"  \n  {rev_note(i)}" if i.get("rev") else ""
                msg = msg.replace("|", "\\|")
                out.append(f"- **{etype}**: {msg}" + (f" — `{where}`" if where else "") +
                           f"{flags}  \n  {i['win']} in window (avg {i['win_avg']:.0f}/day vs "
                           f"{i['base_avg']:.1f}/day baseline{trend}), created {i['created']} — "
                           f"[airbrake]({link(r['pid'], i)}){spread}{note}")
    quiet = [PROJECTS[r["pid"]] for r in results
             if "error" not in r and not any(r[k] for k in ("new", "reappeared", "spike", "respiked"))]
    if quiet:
        out += ["", f"Nothing unusual: {', '.join(quiet)}."]
    return "\n".join(out) + "\n"


def bars(values, highlight, w, h, labels=None):
    """Inline SVG bar sparkline; the last `highlight` bars (the window) are drawn in the accent colour."""
    n, top = len(values), max(values) or 1
    gap = 2 if w / n > 5 else 1
    bw = (w - gap * (n - 1)) / n
    out = [f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="daily notices, last {n} days">']
    for idx, v in enumerate(values):
        bh = max(1.5, v / top * (h - 2)) if v else 1
        x = idx * (bw + gap)
        cls = "bar win" if idx >= n - highlight else "bar"
        tip = f"{labels[idx]}: {v:,}" if labels else f"{v:,}"
        out.append(f'<g><rect class="hit" x="{x:.1f}" y="0" width="{bw + gap:.1f}" height="{h}"/>'
                   f'<rect class="{cls}" x="{x:.1f}" y="{h - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1"/>'
                   f'<title>{html.escape(tip)}</title></g>')
    out.append("</svg>")
    return "".join(out)


def fmt(n):
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{n:,.0f}"


def render_html(date, win_days, results, prev, generated):
    e = html.escape
    n_win = len(win_days)
    ok = [r for r in results if "error" not in r]
    attention = sum(len(r[k]) for r in ok for k, *_, a in KINDS if a)
    fresh = sum(is_new(i, prev) for r in ok for k, *_, a in KINDS if a for i in r[k])
    hidden = sum(len(r["reviewed"]) for r in ok)
    window = win_days[0] if n_win == 1 else f"{win_days[0]} → {win_days[-1]}"

    cards = []
    for r in results:
        name = PROJECTS[r["pid"]]
        if "error" in r:
            cards.append(f'<a class="card" href="#p{r["pid"]}"><div class="card-name">{e(name)}</div>'
                         f'<div class="card-err">{e(r["error"])}</div></a>')
            continue
        ratio = r["win_avg"] / r["base_avg"] if r["base_avg"] else 0
        tone = "crit" if ratio >= 2 else "warn" if ratio >= 1.3 else "ok"
        counts = [(k, short, len(r[k])) for k, _, short, a in KINDS if a or k == "elevated"]
        pills = "".join(f'<span class="pill k-{k}">{c} {e(short.lower())}</span>' for k, short, c in counts if c) \
            or '<span class="pill k-clear">all clear</span>'
        labels = [d for d, _ in r["series"]]
        cards.append(
            f'<a class="card" href="#p{r["pid"]}">'
            f'<div class="card-name">{e(name)}</div>'
            f'<div class="card-row"><div class="card-num">{fmt(r["win"])}</div>'
            f'<div class="ratio t-{tone}">{ratio:.1f}× <span>vs {BASELINE_DAYS}d avg</span></div></div>'
            f'{bars([v for _, v in r["series"]], n_win, 260, 40, labels)}'
            f'<div class="pills">{pills}</div></a>')

    sections = []
    for r in results:
        if "error" in r:
            continue
        name = PROJECTS[r["pid"]]
        blocks = []
        for k, label, short, important in KINDS:
            items = r[k]
            if not items:
                continue
            rows = []
            for i in items:
                g = i["g"]
                etype, msg, where = parts(g)
                since = ""
                if prev and k not in ("reviewed", "ignored"):
                    since = (f'<span class="tag fresh">new since {e(prev["date"])}</span>' if not i["seen"]
                             else f'<span class="tag">also on {e(prev["date"])}</span>')
                tags = since + "".join(f'<span class="tag">{t}</span>' for t in
                                       (["muted"] if g.get("muted") else []) +
                                       (["resolved"] if g.get("resolved") else []) +
                                       ([f"×{len(i['ids'])} groups"] if len(i["ids"]) > 1 else []))
                b = i.get("breakdown")
                spread = (f'<div class="breakdown"><b>{e(scope(b))}</b> · '
                        + e(", ".join(f"{b['label']} {v} ×{c}" for v, c in b["top"]))
                        + f' <span class="muted">({b["sampled"]} notices sampled)</span></div>') if b else ""
                note = f'<div class="revnote">{e(rev_note(i))}</div>' if i.get("rev") else ""
                data = (f' data-sig="{i["sig"]}" data-pid="{r["pid"]}" data-kind="{k}"'
                        f' data-ids="{e(json.dumps([str(x) for x in i["ids"]]))}"'
                        f' data-label="{e(f"{etype}: {msg}"[:160])}"'
                        f' data-win-avg="{i["win_avg"]:.2f}" data-base-avg="{i["base_avg"]:.2f}"')
                check = ('<label class="rev"><input type="checkbox"'
                         + (" checked" if k == "reviewed" else "") + '> Reviewed</label>') if k != "ignored" else ""
                rows.append(
                    f'<div class="item k-{k}{" attn" if important else ""}{" done" if k == "reviewed" else ""}"{data}>'
                    f'<div class="item-main">'
                    f'<div class="item-head"><span class="badge k-{k}">{e(short)}</span>'
                    f'<code class="etype">{e(etype)}</code>{tags}</div>'
                    f'<div class="msg">{e(msg)}</div>'
                    + (f'<div class="where">{e(where)}</div>' if where else "") +
                    f'{spread}{note}'
                    f'<div class="meta"><span><b>{i["win"]:,}</b> in window</span>'
                    f'<span><b>{i["win_avg"]:,.0f}</b>/day vs <b>{i["base_avg"]:,.1f}</b>/day baseline</span>'
                    f'<span>created {e(i["created"])}</span></div></div>'
                    f'<div class="item-side">{bars(i["trend"], n_win, 140, 34)}'
                    f'<div class="spark-cap">last {TREND_DAYS} days</div>'
                    f'{check}'
                    f'<a class="open" href="{e(link(r["pid"], i))}" target="_blank" rel="noopener">Open in Airbrake ↗</a>'
                    f'</div></div>')
            body = "".join(rows)
            if important:
                blocks.append(f'<h3>{e(label)} <span class="count">{len(items)}</span></h3>{body}')
            else:
                blocks.append(f'<details><summary>{e(label)} <span class="count">{len(items)}</span></summary>'
                              f'{body}</details>')
        flagged = any(r[k] for k, *_, a in KINDS if a)
        status = "" if flagged else '<div class="clear">✓ Nothing unusual</div>'
        sections.append(f'<section id="p{r["pid"]}"><h2>{e(name)}<span class="sub">{r["active"]} active groups · '
                        f'{r["win"]:,} notices</span></h2>{status}{"".join(blocks)}</section>')

    summary = (f'<span class="t-crit">●</span> <span id="left">{attention}</span> of {attention} '
               f'item{"s" * (attention != 1)} need a look'
               if attention else '<span class="t-ok">●</span> Nothing needs attention')
    extra = " · ".join(([f"{fresh} new since the {e(prev['date'])} report"] if prev else [])
                       + [f"{hidden} reviewed group{'s' * (hidden != 1)} hidden"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Airbrake Daily Check</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f5f5f3; --surface: #fcfcfb; --border: #e4e3df; --text: #0b0b0b; --text-2: #52514e; --muted: #7a7974;
  --accent: #2a78d6; --bar: #c9c8c2; --hover: #f0efec;
  --crit: #d03b3b; --serious: #ec835a; --warn: #c98500; --good: #0ca30c; --info: #6250d6;
  --crit-bg: #fbeaea; --serious-bg: #fdf0ea; --warn-bg: #fdf5e3; --good-bg: #e9f6e9; --info-bg: #eeecfb;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --border: #2e2e2b; --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
    --accent: #3987e5; --bar: #45443f; --hover: #232321;
    --crit: #e66767; --serious: #ec835a; --warn: #fab219; --good: #2fbf2f; --info: #9085e9;
    --crit-bg: #3a1f1f; --serious-bg: #3a261d; --warn-bg: #362b12; --good-bg: #17301a; --info-bg: #25223d;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --border: #2e2e2b; --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
  --accent: #3987e5; --bar: #45443f; --hover: #232321;
  --crit: #e66767; --serious: #ec835a; --warn: #fab219; --good: #2fbf2f; --info: #9085e9;
  --crit-bg: #3a1f1f; --serious-bg: #3a261d; --warn-bg: #362b12; --good-bg: #17301a; --info-bg: #25223d;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 1080px; margin: 0 auto; padding: 32px 16px 64px; }}
header {{ display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 24px; }}
h1 {{ font-size: 26px; margin: 0; letter-spacing: -0.02em; }}
.subtitle {{ color: var(--text-2); margin-top: 4px; }}
.headline {{ font-weight: 600; font-size: 15px; background: var(--surface); border: 1px solid var(--border);
  padding: 8px 14px; border-radius: 999px; }}
code, .where, .etype {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 36px; }}
.card {{ display: block; background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px; color: inherit; text-decoration: none; transition: border-color .15s, transform .15s; }}
.card:hover {{ border-color: var(--accent); transform: translateY(-1px); }}
.card-name {{ font-weight: 600; color: var(--text-2); font-size: 13px; }}
.card-row {{ display: flex; align-items: baseline; justify-content: space-between; margin: 2px 0 8px; }}
.card-num {{ font-size: 28px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
.card-err {{ color: var(--crit); margin-top: 8px; }}
.ratio {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
.ratio span {{ color: var(--muted); font-weight: 400; font-size: 12px; }}
.t-ok {{ color: var(--good); }} .t-warn {{ color: var(--warn); }} .t-crit {{ color: var(--crit); }}
.spark {{ display: block; max-width: 100%; height: auto; overflow: visible; }}
.spark .bar {{ fill: var(--bar); }} .spark .bar.win {{ fill: var(--accent); }}
.spark .hit {{ fill: transparent; }} .spark g:hover .bar {{ opacity: .7; }}
.pills {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
.pill {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
.k-new.pill, .badge.k-new {{ background: var(--crit-bg); color: var(--crit); }}
.k-reappeared.pill, .badge.k-reappeared {{ background: var(--serious-bg); color: var(--serious); }}
.k-spike.pill, .badge.k-spike {{ background: var(--warn-bg); color: var(--warn); }}
.k-elevated.pill, .badge.k-elevated {{ background: var(--info-bg); color: var(--info); }}
.k-respiked.pill, .badge.k-respiked {{ background: var(--crit-bg); color: var(--crit); }}
.badge.k-reviewed {{ background: var(--good-bg); color: var(--good); }}
.k-clear.pill {{ background: var(--good-bg); color: var(--good); }}
.badge.k-recent, .badge.k-ignored {{ background: var(--hover); color: var(--text-2); }}
section {{ margin-bottom: 40px; scroll-margin-top: 16px; }}
h2 {{ font-size: 19px; margin: 0 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; }}
h2 .sub {{ font-size: 13px; font-weight: 400; color: var(--muted); }}
h3, summary {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-2); margin: 20px 0 8px; }}
summary {{ cursor: pointer; list-style-position: inside; }}
details {{ margin-top: 12px; }}
.count {{ background: var(--hover); border-radius: 999px; padding: 0 8px; font-size: 12px; margin-left: 4px; }}
.clear {{ color: var(--good); font-weight: 600; }}
.item {{ display: flex; gap: 20px; justify-content: space-between; background: var(--surface);
  border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 8px; }}
.item.k-new {{ border-left-color: var(--crit); }} .item.k-reappeared {{ border-left-color: var(--serious); }}
.item.k-spike {{ border-left-color: var(--warn); }} .item.k-elevated {{ border-left-color: var(--info); }}
.item.k-respiked {{ border-left-color: var(--crit); }}
.item.done {{ opacity: .5; border-left-color: var(--good); }}
.rev {{ display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; color: var(--text-2);
  cursor: pointer; user-select: none; }}
.rev input {{ width: 16px; height: 16px; margin: 0; accent-color: var(--good); cursor: pointer; }}
.item.done .rev {{ color: var(--good); }}
body.static .rev {{ opacity: .5; cursor: not-allowed; }}
.tag.fresh {{ color: var(--accent); border-color: var(--accent); font-weight: 600; }}
.revnote {{ font-size: 13px; margin-top: 6px; color: var(--crit); font-weight: 600; }}
.extra {{ color: var(--text-2); font-size: 13px; margin-top: 6px; text-align: right; }}
.banner {{ display: none; background: var(--warn-bg); color: var(--text); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; margin-bottom: 20px; }}
body.static .banner {{ display: block; }}
#toast {{ position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); background: var(--text);
  color: var(--bg); padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s;
  pointer-events: none; }}
#toast.show {{ opacity: 1; }}
.item-main {{ min-width: 0; flex: 1; }}
.item-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
.badge {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
  padding: 2px 8px; border-radius: 6px; }}
.etype {{ font-weight: 600; font-size: 13px; }}
.tag {{ font-size: 11px; color: var(--muted); border: 1px solid var(--border); border-radius: 6px; padding: 0 6px; }}
.msg {{ margin: 6px 0 2px; overflow-wrap: anywhere; }}
.where {{ font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }}
.muted {{ color: var(--muted); }}
.breakdown {{ font-size: 13px; margin-top: 6px; color: var(--text-2); }}
.meta {{ display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--muted); margin-top: 8px;
  font-variant-numeric: tabular-nums; }}
.meta b {{ color: var(--text); font-weight: 600; }}
.item-side {{ flex: 0 0 140px; display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }}
.spark-cap {{ font-size: 11px; color: var(--muted); }}
.open {{ margin-top: auto; font-size: 13px; font-weight: 600; color: var(--accent); text-decoration: none; white-space: nowrap; }}
.open:hover {{ text-decoration: underline; }}
footer {{ color: var(--muted); font-size: 12px; border-top: 1px solid var(--border); padding-top: 16px; }}
@media (max-width: 640px) {{
  .item {{ flex-direction: column; gap: 10px; }}
  .item-side {{ flex-basis: auto; align-items: flex-start; }}
}}
</style>
</head>
<body>
<main>
<header>
  <div><h1>Airbrake daily check</h1>
  <div class="subtitle">{e(date)} · window {e(window)} (UTC) · environment {e("/".join(PROD_ENVS))} only · compared with the previous {BASELINE_DAYS} days</div></div>
  <div><div class="headline">{summary}</div><div class="extra">{extra}</div></div>
</header>
<div class="banner">Opened as a file, so ticks can't be saved. Run <code>python3 check.py --serve</code>
  and open <a href="http://127.0.0.1:{PORT}/{e(date)}.html">http://127.0.0.1:{PORT}/{e(date)}.html</a>.</div>
<div class="grid">{"".join(cards)}</div>
{"".join(sections)}
<footer>
  Spike = window daily avg ≥ {SPIKE_RATIO:g}× the {BASELINE_DAYS}-day baseline (counted from the group's creation), ≥ {SPIKE_MIN_NOTICES}
  notices, and ≥ {SPIKE_RATIO:g}× the previous 7 days. Blue bars are the window. Errors matching AIRBRAKE_IGNORE
  are listed under “Known / ignored”. Ticking “Reviewed” hides a group from later reports until its
  daily average reaches {SPIKE_RATIO:g}× the rate it had when ticked. Generated {e(generated)} UTC.
</footer>
<div id="toast" role="status"></div>
</main>
<script>
const GENERATED = {json.dumps(generated)}, REPORT = {json.dumps(date)};
const live = location.protocol.startsWith("http");
const toast = msg => {{ const t = document.getElementById("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 1800); }};
const left = document.getElementById("left");
const recount = () => {{ if (left) left.textContent = document.querySelectorAll(".item.attn:not(.done)").length; }};
const boxes = [...document.querySelectorAll(".rev input")];
if (!live) {{ document.body.classList.add("static"); boxes.forEach(b => b.disabled = true); }}
else fetch("/api/reviewed").then(r => r.json()).then(rev => {{
  for (const b of boxes) {{
    const d = b.closest(".item").dataset, entry = rev[d.sig];
    b.checked = d.kind === "reviewed" ? !!entry : !!entry && entry.reviewed_at > GENERATED;
    b.closest(".item").classList.toggle("done", b.checked);
  }}
  recount();
}});
boxes.forEach(b => b.addEventListener("change", async () => {{
  const item = b.closest(".item"), d = item.dataset;
  item.classList.toggle("done", b.checked); recount();
  try {{
    const r = await fetch("/api/review", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ sig: d.sig, reviewed: b.checked, pid: +d.pid, ids: JSON.parse(d.ids), label: d.label,
        kind: d.kind, win_avg: +d.winAvg, base_avg: +d.baseAvg, report: REPORT }}) }});
    if (!r.ok) throw new Error(r.status);
    toast(b.checked ? "Marked reviewed — hidden from the next report" : "Review removed");
  }} catch (err) {{
    b.checked = !b.checked; item.classList.toggle("done", b.checked); recount();
    toast("Couldn't save — is check.py --serve running?");
  }}
}}));
</script>
</body>
</html>
"""


def serve(port):
    """Serve reports/ and record the report's "Reviewed" ticks in reviewed.json."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=REPORTS, **k)

        def send_json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/reviewed":
                return self.send_json(load_reviewed())
            if self.path == "/":
                pages = [f for f in os.listdir(REPORTS) if f.endswith(".html")] if os.path.isdir(REPORTS) else []
                if not pages:
                    return self.send_error(404, "No reports yet - run check.py first")
                latest = max(pages)
                self.send_response(302)
                self.send_header("Location", f"/{latest}")
                return self.end_headers()
            super().do_GET()

        def do_POST(self):
            if self.path != "/api/review":
                return self.send_error(404)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rev = load_reviewed()
            if body.pop("reviewed"):
                rev[body["sig"]] = {**body, "reviewed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}
            else:
                rev.pop(body["sig"], None)
            with open(REVIEWED + ".tmp", "w") as f:
                json.dump(rev, f, indent=1)
            os.replace(REVIEWED + ".tmp", REVIEWED)
            self.send_json(rev.get(body["sig"]))

    print(f"Serving reports on http://127.0.0.1:{port}/ (Ctrl-C to stop)", file=sys.stderr)
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", metavar="FILE", help="config file (default: .env next to check.py)")
    ap.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--days", type=int)
    ap.add_argument("--serve", nargs="?", const=PORT, type=int, metavar="PORT")
    a = ap.parse_args()
    if a.serve:
        return serve(a.serve)
    if not KEY:
        sys.exit("No Airbrake key: set AIRBRAKE_KEY (or AIRBRAKE_KEY_FILE) in .env. See README.md.")
    if not PROJECTS:
        sys.exit("No projects: set AIRBRAKE_PROJECTS in .env. See README.md.")
    for pid, name in PROJECTS.items():
        if not name:
            try:
                PROJECTS[pid] = get(f"v4/projects/{pid}")["project"]["name"]
            except Exception:
                PROJECTS[pid] = f"Project {pid}"
    reviewed = load_reviewed()
    os.makedirs(REPORTS, exist_ok=True)
    prev = previous_report(str(a.date))
    since_prev = (a.date - dt.date.fromisoformat(prev["date"])).days if prev else 0
    n = a.days or since_prev or (3 if a.date.weekday() == 0 else 1)
    win_days = [str(a.date - dt.timedelta(days=i)) for i in range(n, 0, -1)]
    base_days = [str(a.date - dt.timedelta(days=i)) for i in range(n + BASELINE_DAYS, n, -1)]

    def run(pid):
        try:
            return check_project(pid, win_days, base_days, reviewed)
        except Exception as e:
            return {"pid": pid, "error": str(e)}

    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(run, PROJECTS))

    for r in results:
        if "error" in r:
            continue
        for k, *_ in KINDS:
            for i in r[k]:
                i["seen"] = bool(prev) and (i["sig"] in prev["sigs"] or
                                            any(f"{r['pid']}:{g}" in prev["ids"] for g in i["ids"]))
            r[k].sort(key=lambda i: (i["seen"], -i["win"]))

    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    snapshot = {"date": str(a.date), "win_days": win_days, "generated": generated, "items": [
        {"pid": r["pid"], "kind": k, "sig": i["sig"], "ids": [str(g) for g in i["ids"]], "win": i["win"],
         "win_avg": i["win_avg"], "base_avg": i["base_avg"], "label": "%s: %s" % parts(i["g"])[:2]}
        for r in results if "error" not in r for k, *_ in KINDS if k != "ignored" for i in r[k]]}
    md = render_md(str(a.date), win_days, results, prev)
    with open(os.path.join(REPORTS, f"{a.date}.md"), "w") as f:
        f.write(md)
    with open(os.path.join(REPORTS, f"{a.date}.html"), "w") as f:
        f.write(render_html(str(a.date), win_days, results, prev, generated))
    with open(os.path.join(REPORTS, f"{a.date}.json"), "w") as f:
        json.dump(snapshot, f, indent=1)
    sys.stdout.write(md)
    print(f"\nSaved {REPORTS}/{a.date}.html, .md and .json", file=sys.stderr)


if __name__ == "__main__":
    main()
