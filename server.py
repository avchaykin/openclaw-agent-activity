import json
import os
import time
from collections import Counter, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_MONITOR_PORT", "8124"))
SESSIONS_DIR = Path(os.environ.get("OPENCLAW_SESSIONS_DIR", "/Users/chay/.openclaw/agents/main/sessions"))
ACTIVE_AGE_HOURS = float(os.environ.get("AGENT_MONITOR_ACTIVE_HOURS", "24"))
TAIL_LINES = int(os.environ.get("AGENT_MONITOR_TAIL_LINES", "700"))
RECENT_IDLE_SEC = int(os.environ.get("AGENT_MONITOR_RECENT_SEC", "25"))


def now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def normalize_ts(ts):
    if ts is None:
        return None
    try:
        ts = float(ts)
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        return ts
    except Exception:
        return None


def ts_to_iso(ts):
    ts = normalize_ts(ts)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def session_files():
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in SESSIONS_DIR.iterdir():
        if not p.is_file():
            continue
        if not p.name.endswith(".jsonl"):
            continue
        if ".jsonl.reset." in p.name:
            continue
        age_sec = time.time() - p.stat().st_mtime
        if age_sec <= ACTIVE_AGE_HOURS * 3600:
            out.append(p)
    out.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return out


def tail_jsonl(path: Path, max_lines: int):
    lines = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
    except Exception:
        return []

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def extract_text(content):
    if not isinstance(content, list):
        return ""
    chunks = []
    for part in content:
        if part.get("type") == "text":
            txt = part.get("text") or ""
            if txt:
                chunks.append(txt)
    return "\n".join(chunks).strip()


def short_tool_details(tool_name, arguments):
    arguments = arguments or {}
    if tool_name == "exec":
        cmd = arguments.get("command") or ""
        cmd = " ".join(str(cmd).split())
        if len(cmd) > 140:
            cmd = cmd[:140] + "…"
        wd = arguments.get("workdir")
        return f"{cmd}" + (f" @ {wd}" if wd else "")
    if tool_name == "read":
        p = arguments.get("path") or arguments.get("file_path") or ""
        return f"path={p}"
    if tool_name == "write":
        p = arguments.get("path") or arguments.get("file_path") or ""
        return f"path={p}"
    if tool_name == "edit":
        p = arguments.get("path") or arguments.get("file_path") or ""
        return f"path={p}"
    if tool_name == "browser":
        act = arguments.get("action") or ""
        return f"action={act}"
    keys = list(arguments.keys())[:3]
    if not keys:
        return ""
    return ", ".join(keys)


def summarize_session(path: Path):
    events = tail_jsonl(path, TAIL_LINES)
    messages = [e for e in events if e.get("type") == "message"]

    role_counts = Counter()
    latest_model = None
    last_user_ts = None
    last_assistant_ts = None
    last_user_text = ""
    last_assistant_text = ""
    last_role = None
    last_message_ts = None

    tool_runs = {}
    tool_runs_order = []
    user_requests = []

    for e in messages:
        m = e.get("message") or {}
        role = m.get("role")
        role_counts[role] += 1

        evt_ts = normalize_ts(m.get("timestamp") if m.get("timestamp") is not None else e.get("timestamp"))
        last_role = role or last_role
        last_message_ts = evt_ts or last_message_ts

        if role == "assistant" and m.get("model"):
            latest_model = m.get("model")

        content = m.get("content") or []

        if role == "assistant":
            txt = extract_text(content)
            if txt:
                last_assistant_text = txt
                last_assistant_ts = evt_ts or last_assistant_ts

            for part in content:
                if part.get("type") != "toolCall":
                    continue
                call_id = part.get("id") or part.get("callId") or part.get("toolCallId") or f"anon-{len(tool_runs_order)+1}"
                tool_name = part.get("name") or "unknown"
                details = short_tool_details(tool_name, part.get("arguments") or {})
                run = {
                    "id": call_id,
                    "tool": tool_name,
                    "details": details,
                    "started_ts": evt_ts,
                    "ended_ts": None,
                    "status": "running",
                    "duration_sec": None,
                }
                tool_runs[call_id] = run
                tool_runs_order.append(call_id)

        elif role == "toolResult":
            call_id = m.get("toolCallId") or m.get("id")
            tool_name = m.get("toolName") or "unknown"
            is_error = bool(m.get("isError"))

            run = tool_runs.get(call_id)
            if run is None:
                run = {
                    "id": call_id or f"orphan-{len(tool_runs_order)+1}",
                    "tool": tool_name,
                    "details": "",
                    "started_ts": None,
                    "ended_ts": evt_ts,
                    "status": "error" if is_error else "ok",
                    "duration_sec": None,
                }
                tool_runs[run["id"]] = run
                tool_runs_order.append(run["id"])
            else:
                run["ended_ts"] = evt_ts
                run["status"] = "error" if is_error else "ok"
                if run.get("started_ts") and evt_ts:
                    run["duration_sec"] = max(0.0, round(evt_ts - run["started_ts"], 2))

        elif role == "user":
            txt = extract_text(content)
            if txt:
                last_user_text = txt
                user_requests.append({
                    "id": f"user-{len(user_requests)+1}",
                    "tool": "USER",
                    "details": (txt[:220] + "…") if len(txt) > 220 else txt,
                    "started_ts": evt_ts,
                    "ended_ts": evt_ts,
                    "status": "request",
                    "duration_sec": None,
                })
            last_user_ts = evt_ts or last_user_ts

    running_calls = [r for r in tool_runs.values() if r["status"] == "running"]

    # Status inference: avoid stale "responding" states.
    now = time.time()
    status = "idle"
    status_label = "Idle"

    if running_calls:
        status = "running_tools"
        status_label = "Running tools"
    elif last_role == "user" and (last_assistant_ts is None or (last_user_ts or 0) > (last_assistant_ts or 0)):
        status = "model_request_pending"
        status_label = "Model request pending"
    else:
        if last_assistant_ts and (now - last_assistant_ts) <= RECENT_IDLE_SEC:
            status = "just_replied"
            status_label = "Just replied"
        else:
            status = "idle"
            status_label = "Idle"

    tool_counter = Counter([r["tool"] for r in tool_runs.values()])
    top_tools = [{"tool": t, "count": c} for t, c in tool_counter.most_common(8)]

    execution_log = []
    for call_id in tool_runs_order[-28:]:
        r = tool_runs.get(call_id)
        if not r:
            continue
        execution_log.append(
            {
                "id": r["id"],
                "tool": r["tool"],
                "details": r.get("details") or "",
                "started_at": ts_to_iso(r.get("started_ts")),
                "ended_at": ts_to_iso(r.get("ended_ts")),
                "status": r["status"],
                "duration_sec": r.get("duration_sec"),
                "sort_ts": r.get("started_ts") or r.get("ended_ts") or 0,
            }
        )

    for u in user_requests[-12:]:
        execution_log.append(
            {
                "id": u["id"],
                "tool": u["tool"],
                "details": u["details"],
                "started_at": ts_to_iso(u.get("started_ts")),
                "ended_at": ts_to_iso(u.get("ended_ts")),
                "status": u["status"],
                "duration_sec": None,
                "sort_ts": u.get("started_ts") or 0,
            }
        )

    execution_log.sort(key=lambda x: x.get("sort_ts") or 0)
    for row in execution_log:
        row.pop("sort_ts", None)

    return {
        "session": path.name.replace(".jsonl", ""),
        "file": path.name,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "status_label": status_label,
        "last_activity_at": ts_to_iso(last_message_ts),
        "model": latest_model or "unknown",
        "counts": {
            "user_messages": role_counts.get("user", 0),
            "assistant_messages": role_counts.get("assistant", 0),
            "tool_calls": sum(1 for r in tool_runs.values() if r.get("started_ts") is not None),
            "tool_results": sum(1 for r in tool_runs.values() if r.get("ended_ts") is not None),
            "running_now": len(running_calls),
        },
        "top_tools": top_tools,
        "execution_log": execution_log,
        "last_user": (last_user_text[:180] + "…") if len(last_user_text) > 180 else last_user_text,
        "last_assistant": (last_assistant_text[:180] + "…") if len(last_assistant_text) > 180 else last_assistant_text,
        "model_request": {
            "active": status == "model_request_pending",
            "since": ts_to_iso(last_user_ts) if status == "model_request_pending" else None,
        },
    }


def build_snapshot():
    sessions = [summarize_session(p) for p in session_files()]
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)

    summary = {
        "active_sessions": len(sessions),
        "running_tools": sum(1 for s in sessions if s["status"] == "running_tools"),
        "pending_model_requests": sum(1 for s in sessions if s["status"] == "model_request_pending"),
        "just_replied": sum(1 for s in sessions if s["status"] == "just_replied"),
        "idle": sum(1 for s in sessions if s["status"] == "idle"),
    }

    active_model_requests = [
        {
            "session": s["session"],
            "status": s["status_label"],
            "since": s["model_request"]["since"],
            "model": s["model"],
        }
        for s in sessions
        if s["model_request"]["active"]
    ]

    return {
        "generated_at": now_iso(),
        "summary": summary,
        "active_model_requests": active_model_requests,
        "sessions": sessions,
    }


INDEX_HTML = """<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>OpenClaw Agent Activity Monitor</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; background: #0b0f14; color: #dbe2ea; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
    .wrap { max-width: 1280px; margin: 20px auto; padding: 0 14px 24px; }
    h1 { margin: 0 0 8px; font-size: 1.35rem; }
    .sub { color: #90a0b3; margin-bottom: 14px; }
    .grid { display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .kpi { background: #131a23; border: 1px solid #263140; border-radius: 12px; padding: 10px; }
    .kpi .v { font-size: 1.2rem; font-weight: 700; }
    .card { background: #131a23; border: 1px solid #263140; border-radius: 12px; padding: 12px; margin-bottom: 10px; }
    .row { display:flex; gap: 10px; justify-content: space-between; flex-wrap: wrap; }
    .muted { color: #90a0b3; font-size: .92rem; }
    .pill { padding: 3px 8px; border-radius: 999px; border:1px solid #334155; font-size: .78rem; }
    .st-model_request_pending { border-color: #f59e0b; color: #fbbf24; }
    .st-running_tools { border-color: #3b82f6; color: #93c5fd; }
    .st-just_replied { border-color: #10b981; color: #6ee7b7; }
    .st-idle { border-color: #64748b; color: #cbd5e1; }
    .tools { display:flex; gap:6px; flex-wrap: wrap; margin-top:8px; }
    .tool { background:#0f1720; border:1px solid #2a3646; border-radius: 999px; padding:2px 8px; font-size:.78rem; }
    .sep { height:1px; background:#233040; margin:8px 0; }
    .exec-log { margin-top:8px; border:1px solid #263140; border-radius:10px; overflow:hidden; max-height: 260px; overflow-y: auto; }
    .exec-row { display:grid; grid-template-columns: 145px 90px 1fr 160px 100px; gap:8px; padding:6px 8px; border-top:1px solid #1e2937; font-size:.82rem; align-items:center; }
    .exec-row:first-child { border-top:none; }
    .exec-head { background:#0f1720; color:#9fb0c3; font-weight:600; }
    .st-ok { color:#34d399; }
    .st-error { color:#f87171; }
    .st-running { color:#fbbf24; }
    .st-request { color:#c4b5fd; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    @media (max-width: 980px){
      .grid { grid-template-columns: repeat(2, minmax(140px,1fr)); }
      .exec-row { grid-template-columns: 1fr; }
      .exec-head { display:none; }
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>👻 Agent Activity Monitor</h1>
    <div class=\"sub\" id=\"updated\">Loading...</div>

    <div class=\"grid\">
      <div class=\"kpi\"><div class=\"muted\">Active sessions</div><div class=\"v\" id=\"kpi-sessions\">0</div></div>
      <div class=\"kpi\"><div class=\"muted\">Running tools</div><div class=\"v\" id=\"kpi-tools\">0</div></div>
      <div class=\"kpi\"><div class=\"muted\">Pending model</div><div class=\"v\" id=\"kpi-model\">0</div></div>
      <div class=\"kpi\"><div class=\"muted\">Just replied</div><div class=\"v\" id=\"kpi-replied\">0</div></div>
      <div class=\"kpi\"><div class=\"muted\">Idle</div><div class=\"v\" id=\"kpi-idle\">0</div></div>
    </div>

    <div class=\"card\">
      <div class=\"row\"><strong>Active model requests</strong><span class=\"muted\">status only, no payload</span></div>
      <div id=\"model-reqs\" class=\"muted\">No active requests</div>
    </div>

    <div id=\"sessions\"></div>
  </div>

<script>
const byId = (id) => document.getElementById(id);

function escapeHtml(s=''){
  return String(s).replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
}

function renderModelRequests(items){
  const root = byId('model-reqs');
  if (!items || !items.length){
    root.textContent = 'No active requests';
    return;
  }
  root.innerHTML = items.map(i => `• <b>${escapeHtml(i.session)}</b> · ${escapeHtml(i.status)} · ${escapeHtml(i.model || 'unknown')} · since ${escapeHtml(i.since || '—')}`).join('<br/>');
}

function renderExecRows(execLog){
  if (!execLog || !execLog.length){
    return '<div class="muted" style="padding:8px">No tool executions yet</div>';
  }

  const rows = execLog.slice(-10).reverse().map(x => {
    const statusCls = `st-${escapeHtml(x.status || 'running')}`;
    const dur = x.duration_sec != null ? `${x.duration_sec}s` : '—';
    const start = x.started_at || '—';
    return `<div class="exec-row"><div class="mono">${escapeHtml(start)}</div><div><span class="pill">${escapeHtml(x.tool || '')}</span></div><div class="mono">${escapeHtml(x.details || '')}</div><div class="mono">${escapeHtml(dur)}</div><div class="mono ${statusCls}">${escapeHtml(x.status || '')}</div></div>`;
  }).join('');

  return `<div class="exec-log"><div class="exec-row exec-head"><div>Started</div><div>Type</div><div>Details</div><div>Duration</div><div>Status</div></div>${rows}</div>`;
}

function renderSessions(list){
  const root = byId('sessions');
  if (!list.length){
    root.innerHTML = '<div class="card muted">No active sessions in the selected time window.</div>';
    return;
  }

  root.innerHTML = list.map(s => {
    const tools = (s.top_tools || []).map(t => `<span class="tool">${escapeHtml(t.tool)} ×${t.count}</span>`).join('');

    return `
      <div class="card">
        <div class="row">
          <div><strong>${escapeHtml(s.session)}</strong></div>
          <span class="pill st-${escapeHtml(s.status)}">${escapeHtml(s.status_label)}</span>
        </div>
        <div class="muted">Model: ${escapeHtml(s.model || 'unknown')} · updated: ${escapeHtml(s.updated_at || '—')}</div>
        <div class="muted">Messages: user ${s.counts?.user_messages || 0} · assistant ${s.counts?.assistant_messages || 0} · tool calls ${s.counts?.tool_calls || 0} · tool results ${s.counts?.tool_results || 0} · running ${s.counts?.running_now || 0}</div>
        <div class="sep"></div>
        <div class="muted">Last user: ${escapeHtml(s.last_user || '—')}</div>
        <div class="muted">Last assistant: ${escapeHtml(s.last_assistant || '—')}</div>
        <div class="tools">${tools || '<span class="muted">No tools detected</span>'}</div>
        ${renderExecRows(s.execution_log || [])}
      </div>
    `;
  }).join('');
}

function applyData(data){
  byId('updated').textContent = `Updated: ${data.generated_at}`;
  byId('kpi-sessions').textContent = data.summary?.active_sessions ?? 0;
  byId('kpi-tools').textContent = data.summary?.running_tools ?? 0;
  byId('kpi-model').textContent = data.summary?.pending_model_requests ?? 0;
  byId('kpi-replied').textContent = data.summary?.just_replied ?? 0;
  byId('kpi-idle').textContent = data.summary?.idle ?? 0;
  renderModelRequests(data.active_model_requests || []);
  renderSessions(data.sessions || []);
}

fetch('/api/snapshot').then(r => r.json()).then(applyData).catch(() => {
  byId('updated').textContent = 'Failed to load';
});

const es = new EventSource('/events');
es.onmessage = (ev) => {
  try { applyData(JSON.parse(ev.data)); } catch (_) {}
};
es.onerror = () => {
  // browser auto-reconnects
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/snapshot":
            self._send_json(build_snapshot())
            return

        if parsed.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                while True:
                    payload = json.dumps(build_snapshot(), ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(1.5)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"Agent Activity Monitor: http://{HOST}:{PORT}")
    print(f"Sessions dir: {SESSIONS_DIR}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
