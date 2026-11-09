# Tracker Chat (demo)

A LangGraph agent that talks to two MCP servers over stdio:

- **habit-check**: daily habit check-ins and streaks
- **job-tracker**: job applications and follow-ups

Read tools run straight away. Every write tool pauses the graph (`interrupt()`) until you click **Approve**.

Built with FastMCP 4, LangGraph, langchain-mcp-adapters, FastAPI and a plain HTML page.
Deployed as one Docker container on Render's free plan.

**Demo data only:** the companies are fictional and everything resets when the service restarts
(the free plan has no persistent disk and sleeps after 15 idle minutes; the first visit after that takes 1-2 minutes).
The app is password-protected and has an hourly message cap, because every message calls the OpenAI API.

## Layout

```
client/              FastAPI app (web.py), terminal client (main.py), static/index.html
servers/habit-check/ MCP server
servers/job-tracker/ MCP server
Dockerfile           one container: web app + both servers as stdio subprocesses
render.yaml          Render Blueprint (free web service, health check, env vars)
sync.ps1             copies the needed files from the three source projects
```

## Environment

Secrets (set in Render, never committed): `OPENAI_API_KEY`, `APP_PASSWORD` (at least 12 characters).
Optional: `OPENAI_MODEL` (default `gpt-4o-mini`), `MESSAGES_PER_HOUR` (default 60).

## Run the container locally

```powershell
docker build -t tracker-demo .
docker run --rm -p 7860:7860 -e APP_PASSWORD=choose-a-long-one -e OPENAI_API_KEY=$env:OPENAI_API_KEY tracker-demo
```

Then open http://localhost:7860.
