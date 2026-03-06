# Agent Activity Monitor

Веб-интерфейс для live-обзора активных OpenClaw-сессий:

- статус по каждой сессии (ожидание / выполняет тулы / pending model)
- какие тулы дергаются чаще всего
- последние события tool call / tool result
- блок активных запросов к моделям (только статус, без payload)

## Запуск

```bash
cd /Users/chay/.openclaw/workspace/agent-activity-monitor
python3 server.py
```

Открой: `http://127.0.0.1:8124`

## Переменные окружения

- `AGENT_MONITOR_PORT` — порт (по умолчанию `8124`)
- `OPENCLAW_SESSIONS_DIR` — путь к директории `.jsonl` сессий
- `AGENT_MONITOR_ACTIVE_HOURS` — окно активных сессий (часы, default `24`)
- `AGENT_MONITOR_TAIL_LINES` — сколько последних строк читать из каждой сессии (default `500`)
