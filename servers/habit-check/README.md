# habit-check

A local MCP server (stdio) for daily habit check-ins and streaks.
Data stays on this machine in `%APPDATA%\habit-check\habits.db`. Nothing is ever deleted.

Habits: `leetcode`, `apply`, `gym`, `study`, `sleep_by_1` (edit `HABITS` in `main.py`).

## Setup (PowerShell)

```powershell
cd $HOME\OneDrive\Desktop\habit-check
uv sync
uv run main.py   # starts and waits on stdio; Ctrl+C to stop. Log lines go to stderr.
```

## MCP client config

```json
{
  "mcpServers": {
    "habit-check": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\adide\\OneDrive\\Desktop\\habit-check", "main.py"],
      "env": { "APPDATA": "C:\\Users\\adide\\AppData\\Roaming" }
    }
  }
}
```

LangGraph (`langchain-mcp-adapters`) uses the same command and args with `"transport": "stdio"`.

## Tools

| Tool | Kind |
|---|---|
| `today()` | read |
| `streaks()` | read |
| `check_in(habit, day?, note?)` | **write** |
| `undo_check_in(habit, day?, reason?)` | **write** |

Resource: `habits:///list`.

## Changing habits

`CREATE TABLE IF NOT EXISTS` never changes an existing table, so if you add a habit to
`HABITS`, the server will refuse to start. To start a new database without deleting history:
stop the server, rename `%APPDATA%\habit-check\habits.db` to e.g. `habits-2026-11.db`
(along with its `-wal`/`-shm` files), then restart. Removing a habit needs no action.
Also update the habit list in the `check_in` / `undo_check_in` docstrings.
