# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, stdlib-only Python tool that parses Claude Code's local JSONL session
transcripts (`~/.claude/projects/**`) and reports daily prompt-cache stats (cache
hits/writes, hit rate, estimated cost/savings). Everything lives in
[claude_cache_dashboard.py](claude_cache_dashboard.py) — there is no package structure,
no dependencies, and no test suite.

## Commands

Run directly (no venv/pip install needed — stdlib only):

```bash
python claude_cache_dashboard.py              # one-shot console table + history snapshot
python claude_cache_dashboard.py --serve      # web dashboard on :8080 (auto re-scans every 30s)
python claude_cache_dashboard.py --days 7     # limit lookback window (0 = all history)
python claude_cache_dashboard.py --json       # machine-readable output instead of the table
python claude_cache_dashboard.py --out report.html   # also write a static HTML file
python claude_cache_dashboard.py --snapshot   # record today into history.json and exit (for cron/Task Scheduler)
```

Key flags/env vars (env vars are what `docker-compose.yml` sets): `--dir`/`CLAUDE_PROJECTS_DIR`
(defaults to `~/.claude/projects`), `--history`/`HISTORY_FILE` (defaults to `./data/history.json`),
`--port`/`PORT`, `--refresh`/`REFRESH`, `TZ` (defines when "today" rolls over — don't change once
history exists, or day boundaries shift).

Docker:

```bash
docker compose up -d --build
docker compose logs -f
```

There is no lint/test/build tooling configured in this repo — verify changes by running the
script directly against a real `~/.claude/projects` tree and checking the console/HTML output.

## Architecture

Single pipeline, all in one file:

1. **`iter_usage_records`** walks every `*.jsonl` under the projects root, parses each line as
   JSON, and yields one record per API call that has a `usage` block (dedup'd by message id/uuid
   since Claude Code transcripts can contain repeated entries).
2. **`scan_logs`** buckets those records by *local* calendar date (via `local_date`, which
   converts each record's UTC timestamp to the machine's local timezone) into per-day totals
   (`new_bucket()`: requests, input/cache_write/cache_read/output tokens, cost, savings, per-model
   call counts). **`record_cost`** does the $ math per record using the hardcoded `PRICING` table
   (`match_pricing` substring-matches the model name, falling back to `FALLBACK` sonnet pricing for
   unrecognized models — token counts stay accurate either way, only $ drifts).
3. **History persistence** (`load_history`/`save_history`/`merged_view`) exists because Claude
   Code prunes its own old JSONL files. Every run merges freshly-scanned days on top of
   `history.json`: scanned data always wins for a day still present in the logs (it may have grown
   since the last run), while history fills in days whose logs are already gone. This merge-and-
   persist step is why *running the tool sooner accumulates more permanent history* — it's the
   core design constraint of the whole project.
4. **Output modes** all consume the same `daily` dict from `merged_view`: `print_console` (table),
   `render_html` (dashboard with inline SVG bar chart, no JS/CSS framework), or raw JSON via
   `--json`/`/api/usage.json`.
5. **Serve mode** (`State` + `make_handler` + `serve`) wraps the same pipeline in a
   `ThreadingHTTPServer` with a 30s TTL cache (`State.get`) so concurrent requests don't re-scan
   the filesystem; routes are `/` (HTML), `/api/usage.json`, `/healthz`.

When changing pricing, edit the `PRICING` dict only — nothing else needs touching. When adding a
new output field, add it to `FIELDS`, `new_bucket()`, `merge_into`, and the relevant
render/print function.
