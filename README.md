# Claude Code Cache Dashboard

Tracks your daily Claude Code prompt-cache stats — cache hits, cache writes, hit
rate, estimated cost, and estimated savings from caching — by reading the JSONL
session transcripts Claude Code already writes locally.

Everything runs on your machine. Nothing is uploaded anywhere. Stdlib Python
only, so the image has no dependencies to install.

---

## Quick start (Docker)

```bash
docker compose up -d --build
```

Open **http://localhost:8080**.

If `${USERPROFILE}` doesn't expand on Windows, edit the volume line in
`docker-compose.yml` to hardcode your path:

```yaml
- "C:/Users/YOURNAME/.claude/projects:/logs:ro"
```

Note the forward slashes — Docker Desktop wants them even on Windows.

Check it found your logs:

```bash
docker compose logs -f
```

If it says logs not found, the mount path is wrong. Confirm the folder exists:

```powershell
dir $env:USERPROFILE\.claude\projects
```

---

## Running without Docker

```bash
python claude_cache_dashboard.py              # console table + history snapshot
python claude_cache_dashboard.py --serve      # web dashboard on :8080
python claude_cache_dashboard.py --days 7     # last week only
python claude_cache_dashboard.py --json       # machine-readable
python claude_cache_dashboard.py --out report.html
```

---

## Why history matters

Claude Code prunes its own JSONL logs over time. Once that happens the raw data
is gone and no tool can recover it.

Every run of this script snapshots per-day totals into `history.json`. Days that
have been recorded survive log pruning permanently. Days still present in the
logs are re-read fresh and overwrite the stored value, so a day that's still in
progress keeps updating rather than freezing at its first snapshot.

Practical upshot: **the sooner you start running it, the more history you keep.**
In Docker the history sits in a named volume (`claude-cache-history`) and
survives rebuilds. Swap that line for `./data:/data` if you'd rather have the
JSON visible on disk.

---

## Keeping it fed

Serve mode re-scans automatically (every 30s, cached between requests), so
`docker compose up -d` with `restart: unless-stopped` is enough — it just keeps
running and history accumulates.

If you'd rather not leave a container up, use snapshot mode on a schedule:

**Windows Task Scheduler** — daily, say 11pm:

```powershell
python C:\path\to\claude_cache_dashboard.py --snapshot
```

**Linux/macOS cron:**

```cron
0 23 * * * /usr/bin/python3 /path/to/claude_cache_dashboard.py --snapshot
```

Snapshot mode writes history and prints one line. Nothing else.

---

## Endpoints (serve mode)

| Path               | Purpose                                    |
| ------------------ | ------------------------------------------ |
| `/`                | HTML dashboard, auto-refreshing            |
| `/api/usage.json`  | Raw per-day data — pipe into anything else |
| `/healthz`         | Health check (used by the container)       |

---

## Configuration

Flags, or environment variables (env vars are what `docker-compose.yml` sets):

| Env                   | Flag        | Default                 | Notes                              |
| --------------------- | ----------- | ----------------------- | ---------------------------------- |
| `CLAUDE_PROJECTS_DIR` | `--dir`     | `~/.claude/projects`    | Where Claude Code writes JSONL     |
| `HISTORY_FILE`        | `--history` | `./data/history.json`   | Persisted per-day totals           |
| `DAYS`                | `--days`    | `30`                    | Lookback window; `0` = all history |
| `PORT`                | `--port`    | `8080`                  |                                    |
| `REFRESH`             | `--refresh` | `60`                    | Browser auto-refresh secs; `0` off |
| `TZ`                  | —           | `Asia/Jakarta`          | Defines when "today" rolls over    |

---

## Reading the numbers

- **Cache read (hit)** — tokens served from cache at ~10% of input price. High is good.
- **Cache write** — tokens written into the cache at ~125% of input price. Some
  is unavoidable; every session has to warm the cache once.
- **Hit rate** — `cache_read / (cache_read + cache_write + fresh_input)`.
  Long agentic Claude Code sessions typically run high, since each turn re-reads
  the whole conversation prefix. A low rate usually means lots of short sessions,
  frequent model switching, or `/compact` and file edits invalidating the prefix.
- **Saved** — what the same tokens would have cost at full input price minus what
  they actually cost.

On a Pro/Max subscription these dollar figures aren't billed to you — treat them
as a measure of how much work you're getting out of the plan, and as a guide to
whether your session habits are cache-friendly.

---

## Caveats

- **Pricing is hardcoded** from Anthropic's published per-model rates. Edit the
  `PRICING` dict at the top of the script if rates change. An unrecognized model
  falls back to Sonnet pricing — token counts and hit rate stay accurate
  regardless, only the dollar columns drift.
- **Don't change `TZ` after you've built history.** Day boundaries would shift
  and old entries would no longer line up with new ones.
- Reads only Claude Code CLI logs. Usage from claude.ai or direct API calls isn't
  included — check claude.ai/settings/usage or the Console for those.
