# Agent Activity Monitor

A real-time web interface for active OpenClaw sessions.

## What it shows

- per-session status (`Idle`, `Running tools`, `Model request pending`, `Just replied`)
- top tools used in each session
- active model requests (status only, no payload/details)
- one-line execution log (start time → final status on the same row)
- user-request markers inside the log (`USER` entries)

## Run

```bash
cd /Users/chay/.openclaw/workspace/tmp/openclaw-agent-activity
python3 server.py
```

Open:

- local: `http://127.0.0.1:8124`
- LAN: `http://<your-ip>:8124`

## Environment variables

- `AGENT_MONITOR_PORT` — server port (default: `8124`)
- `OPENCLAW_SESSIONS_DIR` — path to OpenClaw session `.jsonl` files
- `AGENT_MONITOR_ACTIVE_HOURS` — active-session time window in hours (default: `24`)
- `AGENT_MONITOR_TAIL_LINES` — number of recent lines to parse from each session file (default: `700`)
- `AGENT_MONITOR_RECENT_SEC` — seconds to keep `Just replied` state before switching to `Idle` (default: `25`)
