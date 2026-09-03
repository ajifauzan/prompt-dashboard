#!/usr/bin/env python3
"""
Claude Code Cache Dashboard — daily tracker
===========================================

Reads Claude Code's local session transcripts (JSONL under
~/.claude/projects/**) and reports prompt-cache hit/write stats per day:
cache reads, cache writes, hit rate, estimated cost, and estimated
savings from caching.

Built for continuous daily tracking:

  * HISTORY  Claude Code prunes old JSONL files. Every run snapshots
             per-day totals into history.json, so once a day is
             recorded it survives log pruning forever.
  * SERVE    Runs a tiny HTTP server so you can view the dashboard in a
             browser (essential when running inside Docker).
  * SNAPSHOT One-shot mode for cron / Task Scheduler.

Standard library only — no pip installs.

USAGE
    python claude_cache_dashboard.py                 # console + HTML, one shot
    python claude_cache_dashboard.py --serve         # web server on :8080
    python claude_cache_dashboard.py --snapshot      # record history, no output
    python claude_cache_dashboard.py --days 7        # limit lookback window
    python claude_cache_dashboard.py --json          # machine-readable output
"""

import argparse
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Pricing ($ per million tokens), from Anthropic's prompt caching docs.
#   key -> (base_input, write_5m, write_1h, cache_read, output)
# Edit here if prices change; nothing else needs touching.
# ---------------------------------------------------------------------------
PRICING = {
    "opus-5":     (5.00,  6.25, 10.00, 0.50, 25.00),
    "opus-4-8":   (5.00,  6.25, 10.00, 0.50, 25.00),
    "opus-4-7":   (5.00,  6.25, 10.00, 0.50, 25.00),
    "opus-4-6":   (5.00,  6.25, 10.00, 0.50, 25.00),
    "opus-4-5":   (5.00,  6.25, 10.00, 0.50, 25.00),
    "opus-4-1":   (15.00, 18.75, 30.00, 1.50, 75.00),
    "opus-4":     (15.00, 18.75, 30.00, 1.50, 75.00),
    "sonnet-5":   (2.00,  2.50,  4.00, 0.20, 10.00),
    "sonnet-4-6": (3.00,  3.75,  6.00, 0.30, 15.00),
    "sonnet-4-5": (3.00,  3.75,  6.00, 0.30, 15.00),
    "sonnet-4":   (3.00,  3.75,  6.00, 0.30, 15.00),
    "haiku-4-5":  (1.00,  1.25,  2.00, 0.10,  5.00),
    "haiku-3-5":  (0.80,  1.00,  1.60, 0.08,  4.00),
    "fable-5":    (10.00, 12.50, 20.00, 0.25, 50.00),
    "mythos-5":   (10.00, 12.50, 20.00, 0.25, 50.00),
}
FALLBACK = "sonnet-4-6"

FIELDS = ("requests", "input", "cache_write", "cache_read", "output", "cost", "savings")


def new_bucket():
    return {"requests": 0, "input": 0, "cache_write": 0, "cache_read": 0,
            "output": 0, "cost": 0.0, "savings": 0.0, "models": {}}


def merge_into(dst, src):
    for f in FIELDS:
        dst[f] += src.get(f, 0)
    for m, n in (src.get("models") or {}).items():
        dst["models"][m] = dst["models"].get(m, 0) + n


def match_pricing(model_name):
    name = (model_name or "").lower()
    for key in sorted(PRICING, key=len, reverse=True):
        if key in name:
            return PRICING[key]
    return PRICING[FALLBACK]


def record_cost(rec):
    base, w5, w1, read, out = match_pricing(rec["model"])
    detail = rec["cache_creation_detail"] or {}
    if detail:
        w5_tok = detail.get("ephemeral_5m_input_tokens", 0) or 0
        w1_tok = detail.get("ephemeral_1h_input_tokens", 0) or 0
    else:
        w5_tok, w1_tok = rec["cache_creation_input_tokens"], 0

    cost = (rec["input_tokens"] * base + w5_tok * w5 + w1_tok * w1
            + rec["cache_read_input_tokens"] * read
            + rec["output_tokens"] * out) / 1_000_000

    uncached = ((rec["input_tokens"] + rec["cache_creation_input_tokens"]
                 + rec["cache_read_input_tokens"]) * base
                + rec["output_tokens"] * out) / 1_000_000

    return cost, uncached - cost


def hit_rate(b):
    denom = b["cache_read"] + b["cache_write"] + b["input"]
    return (b["cache_read"] / denom * 100) if denom else 0.0


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
def iter_usage_records(root: Path):
    seen = set()
    for f in root.rglob("*.jsonl"):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = msg.get("usage") or obj.get("usage")
            ts = obj.get("timestamp")
            if not isinstance(usage, dict) or not ts:
                continue

            key = msg.get("id") or obj.get("uuid") or (f.name, ts, hash(line))
            if key in seen:
                continue
            seen.add(key)

            yield {
                "timestamp": ts,
                "model": msg.get("model") or "unknown",
                "input_tokens": usage.get("input_tokens", 0) or 0,
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                "output_tokens": usage.get("output_tokens", 0) or 0,
                "cache_creation_detail": usage.get("cache_creation") or {},
            }


def local_date(ts_str):
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date()
    except (ValueError, TypeError):
        return None


def scan_logs(root: Path, days: int):
    """Return {date_iso: bucket} for days currently present in the logs."""
    daily = defaultdict(new_bucket)
    cutoff = (date.today() - timedelta(days=days - 1)) if days else None

    for rec in iter_usage_records(root):
        d = local_date(rec["timestamp"])
        if d is None or (cutoff and d < cutoff):
            continue
        cost, savings = record_cost(rec)
        b = daily[d.isoformat()]
        b["requests"] += 1
        b["input"] += rec["input_tokens"]
        b["cache_write"] += rec["cache_creation_input_tokens"]
        b["cache_read"] += rec["cache_read_input_tokens"]
        b["output"] += rec["output_tokens"]
        b["cost"] += cost
        b["savings"] += savings
        b["models"][rec["model"]] = b["models"].get(rec["model"], 0) + 1
    return dict(daily)


# ---------------------------------------------------------------------------
# History persistence — survives Claude Code pruning its own logs
# ---------------------------------------------------------------------------
def load_history(path: Path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("days", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_history(path: Path, days: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"updated": datetime.now().isoformat(timespec="seconds"), "days": days}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def merged_view(root: Path, days: int, history_path: Path, write: bool = True):
    """
    Scanned logs win for any day still present (they're authoritative and
    may have grown since the last run). History fills in days whose logs
    Claude Code has since pruned.
    """
    history = load_history(history_path)
    scanned = scan_logs(root, days)

    combined = dict(history)
    combined.update(scanned)

    if write and scanned:
        merged_hist = dict(history)
        merged_hist.update(scanned)
        save_history(history_path, merged_hist)

    if days:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        combined = {k: v for k, v in combined.items() if k >= cutoff}

    for b in combined.values():
        b.setdefault("models", {})
    return combined


def totals(daily):
    grand = new_bucket()
    for b in daily.values():
        merge_into(grand, b)
    return grand


def model_rollup(daily):
    models = defaultdict(int)
    for b in daily.values():
        for m, n in (b.get("models") or {}).items():
            models[m] += n
    return dict(models)


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------
def print_console(daily):
    if not daily:
        print("No Claude Code usage records found for the selected window.")
        return

    today = date.today().isoformat()
    t = daily.get(today)
    print()
    if t:
        print(f"TODAY ({today})  "
              f"calls={t['requests']}  hits={t['cache_read']:,}  writes={t['cache_write']:,}  "
              f"hit%={hit_rate(t):.1f}  cost=${t['cost']:.3f}  saved=${t['savings']:.3f}")
    else:
        print(f"TODAY ({today})  no Claude Code activity recorded yet")
    print()

    hdr = (f'{"Date":<12}{"Calls":>7}{"Cache Read":>13}{"Cache Write":>13}'
           f'{"Fresh In":>10}{"Output":>9}{"Hit %":>8}{"Cost $":>9}{"Saved $":>9}')
    print(hdr)
    print("-" * len(hdr))
    for d in sorted(daily):
        b = daily[d]
        mark = " <" if d == today else ""
        print(f"{d:<12}{b['requests']:>7}{b['cache_read']:>13,}{b['cache_write']:>13,}"
              f"{b['input']:>10,}{b['output']:>9,}{hit_rate(b):>7.1f}%"
              f"{b['cost']:>9.3f}{b['savings']:>9.3f}{mark}")
    g = totals(daily)
    print("-" * len(hdr))
    print(f"{'TOTAL':<12}{g['requests']:>7}{g['cache_read']:>13,}{g['cache_write']:>13,}"
          f"{g['input']:>10,}{g['output']:>9,}{hit_rate(g):>7.1f}%"
          f"{g['cost']:>9.3f}{g['savings']:>9.3f}")

    mr = model_rollup(daily)
    if mr:
        print("\nCalls by model:")
        for m, n in sorted(mr.items(), key=lambda kv: -kv[1]):
            print(f"  {m:<42}{n}")


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
def render_html(daily, refresh_secs=0):
    dates = sorted(daily)
    g = totals(daily)
    today = date.today().isoformat()
    t = daily.get(today, new_bucket())

    chart_days = dates[-14:]
    max_val = max((daily[d]["cache_read"] + daily[d]["cache_write"] for d in chart_days), default=1) or 1

    bw, gap, ch = 22, 14, 150
    bars = []
    for i, d in enumerate(chart_days):
        b = daily[d]
        rh = (b["cache_read"] / max_val) * ch
        wh = (b["cache_write"] / max_val) * ch
        x = i * (bw * 2 + gap) + 12
        tip = (f'{d} · {b["cache_read"]:,} read / {b["cache_write"]:,} write '
               f'· {hit_rate(b):.1f}% hit')
        bars.append(
            f'<g><title>{tip}</title>'
            f'<rect x="{x}" y="{ch - rh + 16:.1f}" width="{bw}" height="{rh:.1f}" fill="#5eead4" rx="2"/>'
            f'<rect x="{x + bw}" y="{ch - wh + 16:.1f}" width="{bw}" height="{wh:.1f}" fill="#f472b6" rx="2"/>'
            f'<text x="{x + bw}" y="{ch + 34}" fill="#94a3b8" font-size="10" '
            f'text-anchor="middle">{d[5:]}</text></g>')
    chart_w = max(len(chart_days) * (bw * 2 + gap) + 24, 240)

    day_rows = "".join(
        f'<tr{" class=today" if d == today else ""}><td>{d}</td><td>{daily[d]["requests"]}</td>'
        f'<td class="teal">{daily[d]["cache_read"]:,}</td>'
        f'<td class="pink">{daily[d]["cache_write"]:,}</td>'
        f'<td>{daily[d]["input"]:,}</td><td>{daily[d]["output"]:,}</td>'
        f'<td>{hit_rate(daily[d]):.1f}%</td><td>${daily[d]["cost"]:.3f}</td>'
        f'<td class="green">${daily[d]["savings"]:.3f}</td></tr>'
        for d in sorted(dates, reverse=True)) or "<tr><td colspan='9'>No data yet</td></tr>"

    mr = model_rollup(daily)
    model_rows = "".join(
        f'<tr><td>{m}</td><td>{n}</td></tr>'
        for m, n in sorted(mr.items(), key=lambda kv: -kv[1])) or "<tr><td colspan='2'>No data</td></tr>"

    meta_refresh = f'<meta http-equiv="refresh" content="{refresh_secs}">' if refresh_secs else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">{meta_refresh}
<title>Claude Code Cache Dashboard</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ background:#0b1220; color:#e2e8f0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
        margin:0; padding:28px; }}
 h1 {{ font-size:19px; font-weight:600; margin:0 0 4px; }}
 .sub {{ color:#64748b; font-size:12px; margin-bottom:24px; }}
 .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr)); gap:12px; margin-bottom:12px; }}
 .card {{ background:#111a2e; border:1px solid #1e293b; border-radius:12px; padding:15px 17px; }}
 .card.hi {{ border-color:#334155; background:#141f38; }}
 .label {{ font-size:10px; text-transform:uppercase; letter-spacing:.07em; color:#64748b; }}
 .value {{ font-size:23px; font-weight:700; margin-top:5px; }}
 .teal{{color:#5eead4}} .pink{{color:#f472b6}} .green{{color:#86efac}} .amber{{color:#fbbf24}}
 .band {{ font-size:11px; color:#475569; margin:0 0 26px; }}
 .legend {{ display:flex; gap:16px; font-size:12px; color:#94a3b8; margin:4px 0 10px; }}
 .dot {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:6px; }}
 table {{ width:100%; border-collapse:collapse; font-size:13px; }}
 th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #1e293b; }}
 th {{ color:#64748b; font-weight:500; font-size:10px; text-transform:uppercase; letter-spacing:.05em; }}
 tr.today td {{ background:#131f36; font-weight:600; }}
 section {{ margin-bottom:30px; }}
 h2 {{ font-size:13px; color:#cbd5e1; margin:0 0 10px; font-weight:600; }}
</style></head><body>

<h1>Claude Code — Cache Usage</h1>
<div class="sub">{len(dates)} day(s) tracked · updated {datetime.now().strftime("%Y-%m-%d %H:%M")}
{" · auto-refresh " + str(refresh_secs) + "s" if refresh_secs else ""}</div>

<h2>Today — {today}</h2>
<div class="cards">
  <div class="card hi"><div class="label">Cache Hits</div><div class="value teal">{t['cache_read']:,}</div></div>
  <div class="card hi"><div class="label">Cache Writes</div><div class="value pink">{t['cache_write']:,}</div></div>
  <div class="card hi"><div class="label">Hit Rate</div><div class="value">{hit_rate(t):.1f}%</div></div>
  <div class="card hi"><div class="label">Calls</div><div class="value">{t['requests']}</div></div>
  <div class="card hi"><div class="label">Cost</div><div class="value amber">${t['cost']:.2f}</div></div>
  <div class="card hi"><div class="label">Saved</div><div class="value green">${t['savings']:.2f}</div></div>
</div>
<div class="band">All time tracked · {g['cache_read']:,} hits · {g['cache_write']:,} writes ·
{hit_rate(g):.1f}% hit rate · ${g['cost']:.2f} cost · ${g['savings']:.2f} saved by caching</div>

<section>
 <h2>Last {len(chart_days)} days — cache reads vs writes (tokens)</h2>
 <div class="legend"><span><span class="dot" style="background:#5eead4"></span>Cache read (hit)</span>
 <span><span class="dot" style="background:#f472b6"></span>Cache write</span></div>
 <svg width="{chart_w}" height="{ch + 46}">{''.join(bars)}</svg>
</section>

<section><h2>By day</h2><table>
 <tr><th>Date</th><th>Calls</th><th>Cache Read</th><th>Cache Write</th><th>Fresh In</th>
 <th>Output</th><th>Hit %</th><th>Cost</th><th>Saved</th></tr>{day_rows}</table></section>

<section><h2>Calls by model</h2><table>
 <tr><th>Model</th><th>Calls</th></tr>{model_rows}</table></section>

</body></html>"""


# ---------------------------------------------------------------------------
# Server mode
# ---------------------------------------------------------------------------
class State:
    def __init__(self, root, days, history_path, ttl=30):
        self.root, self.days, self.history_path, self.ttl = root, days, history_path, ttl
        self.lock = threading.Lock()
        self.cached_at, self.daily = 0.0, {}

    def get(self):
        with self.lock:
            if time.time() - self.cached_at > self.ttl:
                self.daily = merged_view(self.root, self.days, self.history_path)
                self.cached_at = time.time()
            return self.daily


def make_handler(state, refresh):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            daily = state.get()
            if path == "/api/usage.json":
                g = totals(daily)
                payload = {"today": date.today().isoformat(),
                           "days": daily,
                           "totals": {k: g[k] for k in FIELDS},
                           "hit_rate": round(hit_rate(g), 2)}
                self._send(json.dumps(payload, indent=2), "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send("ok", "text/plain; charset=utf-8")
            elif path in ("/", "/index.html"):
                self._send(render_html(daily, refresh), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}", flush=True)

    return Handler


def serve(root, days, history_path, host, port, refresh):
    state = State(root, days, history_path)
    state.get()  # warm + snapshot on boot
    httpd = ThreadingHTTPServer((host, port), make_handler(state, refresh))
    print(f"Claude Code cache dashboard on http://localhost:{port}")
    print(f"  logs:    {root}")
    print(f"  history: {history_path}")
    print(f"  json:    http://localhost:{port}/api/usage.json")
    print("Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Claude Code prompt-cache daily tracker")
    p.add_argument("--dir", default=os.environ.get("CLAUDE_PROJECTS_DIR"),
                   help="Claude Code projects dir (default: ~/.claude/projects)")
    p.add_argument("--history", default=os.environ.get("HISTORY_FILE"),
                   help="History JSON path (default: ./data/history.json)")
    p.add_argument("--days", type=int, default=int(os.environ.get("DAYS", "30")),
                   help="Lookback window in days (0 = all tracked history)")
    p.add_argument("--serve", action="store_true", help="Run the web dashboard")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--refresh", type=int, default=int(os.environ.get("REFRESH", "60")),
                   help="Browser auto-refresh seconds in serve mode (0 = off)")
    p.add_argument("--snapshot", action="store_true",
                   help="Record today into history and exit quietly (for cron)")
    p.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    p.add_argument("--out", default=None, help="Also write a static HTML file here")
    args = p.parse_args()

    root = Path(args.dir).expanduser() if args.dir else Path.home() / ".claude" / "projects"
    history_path = (Path(args.history).expanduser() if args.history
                    else Path.cwd() / "data" / "history.json")

    if not root.exists():
        print(f"Claude Code logs not found at: {root}")
        print("Set --dir (or CLAUDE_PROJECTS_DIR), or mount the folder if running in Docker.")
        raise SystemExit(1)

    if args.serve:
        serve(root, args.days, history_path, args.host, args.port, args.refresh)
        return

    daily = merged_view(root, args.days, history_path)

    if args.snapshot:
        t = daily.get(date.today().isoformat(), new_bucket())
        print(f"snapshot {date.today()} · calls={t['requests']} hits={t['cache_read']:,} "
              f"writes={t['cache_write']:,} hit%={hit_rate(t):.1f} -> {history_path}")
        return

    if args.json:
        g = totals(daily)
        print(json.dumps({"today": date.today().isoformat(), "days": daily,
                          "totals": {k: g[k] for k in FIELDS},
                          "hit_rate": round(hit_rate(g), 2)}, indent=2))
    else:
        print_console(daily)

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(daily), encoding="utf-8")
        print(f"\nHTML written to: {out}")


if __name__ == "__main__":
    main()
