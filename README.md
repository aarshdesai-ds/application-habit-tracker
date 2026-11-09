# Tracker Chat: a LangGraph agent over two MCP servers, with human approval for every write

**Live demo:** [add your Render URL here] (password-protected; see [Demo notes](#demo-notes))

Tracker Chat is a small chat assistant for two parts of a job search: **daily habits** (LeetCode, applications, gym, study, sleep) and **job applications** (status, follow-ups). A LangGraph agent reaches both trackers through the **Model Context Protocol (MCP)**. Each tracker is its own MCP server, running as a stdio subprocess.

The key design rule: **the agent can read freely but can never write without you.** Every tool that changes data pauses the graph with `interrupt()` and waits for an **Approve** or **Decline** click.

```
Browser (index.html)
   │  password header, session id
   ▼
FastAPI (client/web.py) ── rate limit, sessions
   │
   ▼
LangGraph agent ── read tool? ──► run it
   │               write tool? ─► interrupt() ─► Approve / Decline ─► run or skip
   │
   ├── MCP stdio ──► habit-check server  (SQLite)
   └── MCP stdio ──► job-tracker server  (SQLite)
```

## What it can do

Example questions:
- "Which applications need a follow-up?"
- "Log my Acme application from today."
- "Move Northwind to interview."
- "What are my streaks this week?"

| Server | Read tools (run immediately) | Write tools (need approval) |
|---|---|---|
| **habit-check** | `today()`, `streaks()` | `check_in(habit, day?, note?)`, `undo_check_in(habit, day?, reason?)` |
| **job-tracker** | `list_applications(status?, company?)`, `followups_due(days=10)` | `add_application(...)`, `update_status(id, status, note?)` |

Resources: `habits:///list` and `jobs:///statuses`. Statuses follow the pipeline order: `applied → oa → interview → offer → rejected → withdrawn`.

## Design decisions

- **Fail-closed approval.** Only the four tools on an explicit `READ_TOOLS` list run without asking. Any other tool needs approval, including a new one added to a server later.
- **Nothing is ever deleted.** Undoing a habit check-in marks the row `undone` and adds the reason to its note. Applications are never removed; they move to `withdrawn` instead.
- **Duplicate warnings instead of silent writes.** Logging the same application or check-in twice returns `duplicate_warning`, and nothing is written.
- **Validated inputs at the server.** Statuses are enforced by a SQL `CHECK` constraint built from the same `STATUSES` tuple the code uses, so the two can't drift apart. Dates, text lengths and links are validated, and `LIKE` searches are escaped. SQLite runs in WAL mode.
- **Prompt-injection hygiene.** The system prompt tells the model that tool results are data, not instructions. Company names and notes can never steer the agent.
- **Guardrails on the loop.** The graph stops after 12 tool steps (`MAX_STEPS`) to prevent runaway tool loops. Rate-limit and quota errors from the model come back as readable messages.

## Demo deployment

The repo builds one Docker container: the FastAPI web app plus both MCP servers as stdio subprocesses. It's deployed on Render's free plan through `render.yaml`, which sets up the health check at `/api/health` and the environment variables.

Safety for a public demo:
- **Password gate.** `APP_PASSWORD` must be at least 12 characters. It's compared in constant time with `hmac.compare_digest`, and wrong attempts are delayed by 1 second.
- **Hourly message cap** (`MESSAGES_PER_HOUR`, default 60), because every message calls the OpenAI API.
- **Session cap.** The oldest session's LangGraph thread is evicted when the limit is reached.
- **Hardened container.** It runs as a non-root user, installs dependencies from `uv.lock` with `UV_FROZEN=1`, and has no API docs endpoints exposed.

### Demo notes

- **Demo data only.** The companies are fictional (Northwind Labs, Acme Analytics, Globex Robotics). Data is seeded on first start and resets whenever the service restarts. The free plan has no persistent disk.
- **Wake-up delay.** The free plan sleeps after 15 idle minutes, so the first visit after that takes 1–2 minutes.

## Run it yourself

### Docker (same as the demo)

```powershell
docker build -t tracker-demo .
docker run --rm -p 7860:7860 -e APP_PASSWORD=choose-a-long-one -e OPENAI_API_KEY=$env:OPENAI_API_KEY tracker-demo
```

Open http://localhost:7860.

### Terminal client (local data)

`client/main.py` is a terminal version of the same agent: write tools ask for `y` before they run. Each server can also be added to any MCP client (see `servers/*/README.md`).

### Environment variables

| Variable | Required | Default |
|---|---|---|
| `OPENAI_API_KEY` | yes | — |
| `APP_PASSWORD` | yes (web app) | — (at least 12 characters) |
| `OPENAI_MODEL` | no | `gpt-4o-mini` |
| `MESSAGES_PER_HOUR` | no | `60` |

Secrets are set in Render's dashboard and are never committed (`sync: false` in `render.yaml`).

**Privacy note:** your messages, the tool list and tool results (such as application notes) are sent to OpenAI.

## Layout

```
client/              FastAPI app (web.py), terminal client (main.py), static/index.html
servers/habit-check/ MCP server: habit check-ins and streaks (SQLite)
servers/job-tracker/ MCP server: job applications and follow-ups (SQLite)
Dockerfile           one container: web app + both servers as stdio subprocesses
render.yaml          Render Blueprint (free web service, health check, env vars)
sync.ps1             copies the needed files from the three source projects
```

## Built with

LangGraph · Model Context Protocol (FastMCP, langchain-mcp-adapters) · OpenAI (`gpt-4o-mini`) · FastAPI · SQLite (aiosqlite) · uv · Docker · Render
