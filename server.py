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
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800&family=Share+Tech+Mono&display=swap');

    :root { color-scheme: dark; }
    body { margin: 0; background: #0b0f14; color: #dbe2ea; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
    .wrap { max-width: 1280px; margin: 20px auto; padding: 0 14px 24px; }

    /* Optional Sci‑Fi skin */
    @keyframes pulseGlow { 0%,100% { box-shadow: 0 0 12px rgba(43,227,255,.12), inset 0 0 0 1px rgba(62,246,255,.08);} 50% { box-shadow: 0 0 22px rgba(43,227,255,.30), inset 0 0 0 1px rgba(120,255,255,.18);} }
    @keyframes hudPan { 0% { background-position: 0 0, 0 0, 0 0; } 100% { background-position: 0 120px, 120px 0, 200px 0; } }
    @keyframes neonFlicker { 0%,19%,21%,23%,80%,100% { opacity: 1; } 20%,22%,81% { opacity: .84; } }
    @keyframes pingDot { 0% { transform: scale(.8); opacity: .8; } 70% { transform: scale(1.35); opacity: 0; } 100% { transform: scale(1.35); opacity: 0; } }

    body.scifi {
      background: radial-gradient(circle at 20% 20%, #132542 0%, #0a0f1d 38%, #06080f 100%);
      color: #c9f7ff;
      font-family: 'Orbitron', system-ui, sans-serif;
      letter-spacing: .2px;
    }
    body.scifi::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(rgba(80,255,255,.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(80,255,255,.04) 1px, transparent 1px),
        repeating-linear-gradient(0deg, rgba(12,20,33,.0), rgba(12,20,33,.0) 2px, rgba(0,0,0,.15) 3px, rgba(0,0,0,.15) 4px);
      background-size: 100% 4px, 4px 100%, 100% 6px;
      mix-blend-mode: screen;
      opacity: .32;
      z-index: 0;
      animation: hudPan 24s linear infinite;
    }
    body.scifi .wrap { position: relative; z-index: 1; }
    body.scifi h1 { font-family: 'Orbitron', sans-serif; text-transform: uppercase; letter-spacing: 1.2px; text-shadow: 0 0 16px rgba(44,227,255,.45); animation: neonFlicker 5s linear infinite; }
    body.scifi .mono { font-family: 'Share Tech Mono', ui-monospace, SFMono-Regular, Menlo, monospace; }
    h1 { margin: 0 0 8px; font-size: 1.35rem; }
    .sub { color: #90a0b3; margin-bottom: 14px; }
    .grid { display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .kpi { background: #131a23; border: 1px solid #263140; border-radius: 12px; padding: 10px; }
    .kpi .v { font-size: 1.2rem; font-weight: 700; }
    .card { background: #131a23; border: 1px solid #263140; border-radius: 12px; padding: 12px; margin-bottom: 10px; }
    body.scifi .kpi,
    body.scifi .card {
      position: relative;
      background: linear-gradient(180deg, rgba(7,24,40,.92), rgba(7,12,24,.92));
      border-color: #2be3ff66;
      animation: pulseGlow 3.4s ease-in-out infinite;
      backdrop-filter: blur(2px);
    }
    body.scifi .kpi::after,
    body.scifi .card::after {
      content: "";
      position: absolute;
      inset: -1px;
      border-radius: inherit;
      padding: 1px;
      background: linear-gradient(120deg, rgba(79,247,255,.5), rgba(92,84,255,.35), rgba(79,247,255,.5));
      -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
      -webkit-mask-composite: xor;
      mask-composite: exclude;
      pointer-events: none;
    }
    body.scifi .muted { color: #81dff0; }
    body.scifi .pill { border-color: #2be3ff77; color: #b9fbff; background: rgba(12,28,49,.55); }
    body.scifi .tool { background:#081a2b; border-color:#2be3ff55; color:#b9fbff; }
    body.scifi #theme-toggle {
      font-family: 'Orbitron', sans-serif;
      color:#c7fbff;
      border-color:#3be8ff88;
      background: linear-gradient(120deg, rgba(8,34,58,.9), rgba(36,32,82,.85));
      box-shadow: 0 0 12px rgba(59,232,255,.24);
      transition: transform .18s ease, box-shadow .2s ease;
    }
    body.scifi #theme-toggle:hover { transform: translateY(-1px); box-shadow: 0 0 20px rgba(59,232,255,.34); }
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
    .exec-log::-webkit-scrollbar { width: 8px; height: 8px; }
    .exec-log::-webkit-scrollbar-thumb { background: #35506a; border-radius: 999px; }
    .exec-row { display:grid; grid-template-columns: 145px 90px 1fr 160px 100px; gap:8px; padding:6px 8px; border-top:1px solid #1e2937; font-size:.82rem; align-items:center; }
    .exec-row:hover { background: rgba(70,110,150,.08); }
    .exec-row:first-child { border-top:none; }
    .exec-head { background:#0f1720; color:#9fb0c3; font-weight:600; }
    .st-ok { color:#34d399; }
    .st-error { color:#f87171; }
    .st-running { color:#fbbf24; }
    .st-request { color:#c4b5fd; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    body.scifi .exec-log { border-color:#2be3ff66; box-shadow: inset 0 0 18px rgba(34,214,255,.1); }
    body.scifi .exec-log::-webkit-scrollbar-thumb { background: linear-gradient(180deg, #2be3ff, #5f67ff); }
    body.scifi .exec-row:hover { background: rgba(43,227,255,.12); }
    body.scifi .st-running { color:#ffd166; text-shadow: 0 0 10px rgba(255,209,102,.6); }
    body.scifi .st-ok { color:#7bffcf; text-shadow: 0 0 10px rgba(123,255,207,.45); }
    body.scifi .st-error { color:#ff8da1; text-shadow: 0 0 10px rgba(255,141,161,.45); }
    body.scifi .st-request { color:#c9b6ff; text-shadow: 0 0 10px rgba(201,182,255,.45); }
    .status-chip { display:inline-flex; align-items:center; gap:6px; }
    .dot { width:7px; height:7px; border-radius:999px; display:inline-block; position:relative; }
    .dot::after { content:""; position:absolute; inset:0; border-radius:999px; animation: pingDot 1.4s infinite; }
    .dot-ok { background:#34d399; } .dot-ok::after { background:#34d39955; }
    .dot-error { background:#f87171; } .dot-error::after { background:#f8717155; }
    .dot-running { background:#fbbf24; } .dot-running::after { background:#fbbf2455; }
    .dot-request { background:#c4b5fd; } .dot-request::after { background:#c4b5fd55; }

    .hud-row { display:none; grid-template-columns: 1fr 240px; gap:10px; margin-bottom:12px; }
    body.scifi .hud-row { display:grid; }
    .mini-bars { display:flex; align-items:flex-end; gap:4px; height:42px; }
    .mini-bars span { width:10px; border-radius:3px 3px 0 0; background: linear-gradient(180deg,#4fd1ff,#4f46e5); animation: barPulse 1.6s ease-in-out infinite; transform-origin: bottom; }
    .mini-bars span:nth-child(2){ animation-delay:.15s;} .mini-bars span:nth-child(3){ animation-delay:.3s;} .mini-bars span:nth-child(4){ animation-delay:.45s;} .mini-bars span:nth-child(5){ animation-delay:.6s;} .mini-bars span:nth-child(6){ animation-delay:.75s;}
    @keyframes barPulse { 0%,100% { transform: scaleY(.35);} 50% { transform: scaleY(1);} }
    .scan-progress { margin-top:8px; height:8px; border-radius:999px; background:#112233; overflow:hidden; border:1px solid #2a3c52; }
    .scan-progress > div { height:100%; width:0%; background:linear-gradient(90deg,#21d4fd,#b721ff); transition:width .35s ease; }

    .glitch-overlay, .cursor-glitch {
      position: fixed; inset:0; pointer-events:none; z-index: 5;
      opacity: 0;
    }
    .glitch-overlay {
      background:
        repeating-linear-gradient(0deg, rgba(255,0,120,.0), rgba(255,0,120,.0) 2px, rgba(255,0,120,.07) 3px, rgba(255,0,120,.0) 4px),
        repeating-linear-gradient(90deg, rgba(0,255,255,.0), rgba(0,255,255,.0) 3px, rgba(0,255,255,.05) 4px, rgba(0,255,255,.0) 5px);
      mix-blend-mode: screen;
      transition: opacity .35s ease;
    }
    .cursor-glitch {
      background: radial-gradient(220px circle at var(--mx,50%) var(--my,50%), rgba(120,255,255,.16), transparent 58%);
      transition: opacity .2s ease;
    }
    body.scifi.glitch-on .glitch-overlay { opacity: var(--g, .15); animation: hudPan 7s linear infinite; }
    body.scifi.glitch-on .cursor-glitch { opacity: .9; }

    @media (max-width: 980px){
      .grid { grid-template-columns: repeat(2, minmax(140px,1fr)); }
      .hud-row { grid-template-columns: 1fr; }
      .exec-row { grid-template-columns: 1fr; }
      .exec-head { display:none; }
    }
  </style>
</head>
<body>
  <div class=\"glitch-overlay\"></div>
  <div class=\"cursor-glitch\"></div>
  <div class=\"wrap\">
    <div class=\"row\" style=\"align-items:center; margin-bottom:6px;\">
      <h1 style=\"margin:0\">Agent Activity Monitor</h1>
      <button id=\"theme-toggle\" class=\"pill\" style=\"background:#0f1720; cursor:pointer;\">Enable Sci‑Fi</button>
    </div>
    <div class=\"sub\" id=\"updated\">Loading...</div>

    <div class=\"hud-row\">
      <div class=\"card\">
        <div class=\"row\"><strong>Signal activity</strong><span class=\"muted\" id=\"activity-label\">idle</span></div>
        <div class=\"mini-bars\" id=\"mini-bars\"><span style=\"height:20%\"></span><span style=\"height:35%\"></span><span style=\"height:50%\"></span><span style=\"height:42%\"></span><span style=\"height:65%\"></span><span style=\"height:30%\"></span></div>
      </div>
      <div class=\"card\">
        <div class=\"muted\">Neural load</div>
        <div class=\"scan-progress\"><div id=\"scan-fill\"></div></div>
      </div>
    </div>

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

<script src="https://unpkg.com/powerglitch@latest/dist/powerglitch.min.js"></script>
<script>
const byId = (id) => document.getElementById(id);
let glitchTargets = [];

function applyTheme(){
  const mode = localStorage.getItem('oaa-theme') || 'default';
  document.body.classList.toggle('scifi', mode === 'scifi');
  const b = byId('theme-toggle');
  if (b) b.textContent = mode === 'scifi' ? 'Disable Sci‑Fi' : 'Enable Sci‑Fi';
  if (mode !== 'scifi') {
    resetPowerGlitch();
    glitchTargets = [];
  }
}

function toggleTheme(){
  const current = localStorage.getItem('oaa-theme') || 'default';
  localStorage.setItem('oaa-theme', current === 'scifi' ? 'default' : 'scifi');
  applyTheme();
}

function escapeHtml(s=''){
  return String(s).replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
}

function resetPowerGlitch(){
  if (!window.PowerGlitch || !glitchTargets.length) return;
  glitchTargets.forEach((el) => {
    try { PowerGlitch.stop(el); } catch (_) {}
  });
}

function applyPowerGlitch(pressure){
  if (!window.PowerGlitch) return;
  if (!document.body.classList.contains('scifi')) {
    resetPowerGlitch();
    return;
  }

  if (!glitchTargets.length) {
    const header = document.querySelector('h1');
    const kpis = Array.from(document.querySelectorAll('.kpi .v')).slice(0, 5);
    const statusPills = Array.from(document.querySelectorAll('.pill')).slice(0, 8);
    glitchTargets = [header, ...kpis, ...statusPills].filter(Boolean);
  }

  resetPowerGlitch();

  const shake = 0.04 + pressure * 0.22;
  const sliceCount = Math.round(4 + pressure * 10);
  const speed = 0.9 - Math.min(0.6, pressure * 0.55);
  const playMode = pressure > 0.55 ? 'always' : 'hover';

  glitchTargets.forEach((el, idx) => {
    PowerGlitch.glitch(el, {
      playMode,
      timing: {
        duration: 1200 + idx * 40,
        iterations: playMode === 'always' ? Infinity : 1,
      },
      glitchTimeSpan: {
        start: 0.08,
        end: 0.88,
      },
      shake: {
        velocity: speed,
        amplitudeX: shake,
        amplitudeY: shake * 0.55,
      },
      slice: {
        count: sliceCount,
        velocity: speed * 0.9,
        minHeight: 0.02,
        maxHeight: 0.12,
        hueRotate: pressure > 0.4,
      },
      pulse: false,
    });
  });
}

function setActivityFX(data){
  const summary = data?.summary || {};
  const pressure = Math.min(1, ((summary.running_tools || 0) * 0.22) + ((summary.pending_model_requests || 0) * 0.28) + ((summary.just_replied || 0) * 0.12));
  const label = pressure > 0.66 ? 'high' : pressure > 0.28 ? 'medium' : 'idle';
  const lbl = byId('activity-label');
  if (lbl) lbl.textContent = label;

  const fill = byId('scan-fill');
  if (fill) fill.style.width = `${Math.round((pressure * 85) + 10)}%`;

  const bars = document.querySelectorAll('#mini-bars span');
  bars.forEach((b, i) => {
    const base = 18 + ((i * 11) % 37);
    const jitter = Math.round(Math.random() * (18 + pressure * 42));
    b.style.height = `${Math.min(100, base + jitter)}%`;
    b.style.animationDuration = `${1.8 - Math.min(.9, pressure)}s`;
  });

  if (document.body.classList.contains('scifi')) {
    document.body.classList.add('glitch-on');
    document.body.style.setProperty('--g', String(0.08 + pressure * 0.45));
  } else {
    document.body.classList.remove('glitch-on');
    document.body.style.setProperty('--g', '0');
  }

  applyPowerGlitch(pressure);
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
    const status = escapeHtml(x.status || 'running');
    const statusCls = `st-${status}`;
    const dotCls = `dot-${status}`;
    const dur = x.duration_sec != null ? `${x.duration_sec}s` : '—';
    const start = x.started_at || '—';
    return `<div class="exec-row"><div class="mono">${escapeHtml(start)}</div><div><span class="pill">${escapeHtml(x.tool || '')}</span></div><div class="mono">${escapeHtml(x.details || '')}</div><div class="mono">${escapeHtml(dur)}</div><div class="mono ${statusCls}"><span class="status-chip"><span class="dot ${dotCls}"></span>${status}</span></div></div>`;
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
  setActivityFX(data);
}

applyTheme();
const themeBtn = byId('theme-toggle');
if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

window.addEventListener('mousemove', (ev) => {
  const x = (ev.clientX / window.innerWidth) * 100;
  const y = (ev.clientY / window.innerHeight) * 100;
  document.body.style.setProperty('--mx', `${x}%`);
  document.body.style.setProperty('--my', `${y}%`);
});

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
