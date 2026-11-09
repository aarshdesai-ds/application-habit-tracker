"""Web frontend for mcp-client: FastAPI + one static HTML page (no build step).

Same LangGraph agent and approval rule as the terminal client (main.py):
read tools run straight away, every other tool waits for an Approve click.
Every /api call except /api/health needs the X-App-Password header (APP_PASSWORD).

Run locally:  uv run uvicorn web:app --port 8000
"""

import asyncio
import hmac
import logging
import os
import sys
import time
import uuid
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import openai
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from pydantic import BaseModel, Field

import main as core  # load_tools, build_graph, DEFAULT_MODEL, MAX_STEPS

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[web] %(message)s")
log = logging.getLogger("web")

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # never overrides variables already set (e.g. Space secrets)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_MESSAGE = 1000          # chars per chat message
MAX_SESSIONS = 100          # browser sessions kept in memory; oldest is dropped
MIN_PASSWORD = 12
MESSAGES_PER_HOUR = int(os.environ.get("MESSAGES_PER_HOUR", "60"))  # caps OpenAI spend
DEMO_MODE = os.environ.get("DEMO_MODE") == "1"


# ---------------------------------------------------------------------------
# Request / response shapes (validated by FastAPI)
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)


class ApproveIn(BaseModel):
    session_id: str
    approve: bool


# ---------------------------------------------------------------------------
# Demo data (only when DEMO_MODE=1; fictional companies)
# ---------------------------------------------------------------------------
async def seed_demo(tools: list) -> None:
    marker = Path(os.environ["APPDATA"]) / "demo-seeded"
    if marker.exists():
        return
    by_name = {t.name: t for t in tools}
    days_ago = lambda n: (date.today() - timedelta(days=n)).isoformat()
    calls = [
        ("add_application", {"company": "Northwind Labs", "role": "ML Engineer Intern", "applied_on": days_ago(20)}),
        ("add_application", {"company": "Acme Analytics", "role": "Data Analyst", "applied_on": days_ago(12)}),
        ("add_application", {"company": "Globex Robotics", "role": "Backend Engineer", "applied_on": days_ago(3)}),
        ("update_status", {"id": 1, "status": "interview", "note": "Phone screen booked"}),
        ("check_in", {"habit": "leetcode", "day": days_ago(2)}),
        ("check_in", {"habit": "leetcode", "day": days_ago(1)}),
        ("check_in", {"habit": "gym", "day": days_ago(1)}),
        ("check_in", {"habit": "apply", "day": days_ago(0)}),
    ]
    for name, args in calls:
        if name in by_name:
            await by_name[name].ainvoke(args)  # server-side seeding: no approval needed
    marker.write_text(date.today().isoformat())
    log.info("Seeded demo data")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def create_app(make_llm=None) -> FastAPI:
    """make_llm(tools) -> llm with tools bound. Tests pass a fake; default is OpenAI."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if len(os.environ.get("APP_PASSWORD", "")) < MIN_PASSWORD:
            raise RuntimeError(f"Set APP_PASSWORD (at least {MIN_PASSWORD} characters) before starting.")
        if make_llm is None and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        async with AsyncExitStack() as stack:
            tools = await core.load_tools(stack)
            if DEMO_MODE:
                await seed_demo(tools)
            if make_llm is None:
                model = os.environ.get("OPENAI_MODEL", core.DEFAULT_MODEL)
                llm = ChatOpenAI(model=model, temperature=0).bind_tools(tools)
            else:
                llm = make_llm(tools)
            app.state.graph = core.build_graph(llm, tools)
            log.info("Ready with %d tools", len(tools))
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    sessions: dict[str, dict] = {}          # session_id -> {"thread": int, "seen": float, "lock": Lock}
    recent_calls: deque[float] = deque()    # timestamps of LLM-using requests (all users)

    # --- guards -----------------------------------------------------------
    async def require_password(x_app_password: str = Header(default="")) -> None:
        expected = os.environ.get("APP_PASSWORD", "")
        if not expected or not hmac.compare_digest(x_app_password.encode(), expected.encode()):
            await asyncio.sleep(1)  # slows down password guessing
            raise HTTPException(401, "Wrong or missing password.")

    def rate_limit() -> None:
        now = time.monotonic()
        while recent_calls and now - recent_calls[0] > 3600:
            recent_calls.popleft()
        if len(recent_calls) >= MESSAGES_PER_HOUR:
            raise HTTPException(429, "The demo's hourly message limit is used up. Try again later.")
        recent_calls.append(now)

    async def get_session(session_id: str | None) -> tuple[str, dict]:
        if session_id in sessions:
            s = sessions[session_id]
        else:
            if len(sessions) >= MAX_SESSIONS:
                oldest = min(sessions, key=lambda k: sessions[k]["seen"])
                old = sessions.pop(oldest)
                await app.state.graph.checkpointer.adelete_thread(f"{oldest}:{old['thread']}")
            session_id = uuid.uuid4().hex
            s = sessions[session_id] = {"thread": 1, "lock": asyncio.Lock()}
        s["seen"] = time.monotonic()
        return session_id, s

    def config_for(session_id: str, s: dict) -> dict:
        return {
            "configurable": {"thread_id": f"{session_id}:{s['thread']}"},
            "recursion_limit": core.MAX_STEPS * 3,
        }

    async def step(session_id: str, s: dict, payload) -> dict:
        """Run the graph until it answers or pauses for approval."""
        try:
            result = await app.state.graph.ainvoke(payload, config_for(session_id, s))
            if result.get("__interrupt__"):
                return {"session_id": session_id, "reply": None, "approval": result["__interrupt__"][0].value}
            content = result["messages"][-1].content
            return {"session_id": session_id, "reply": content if isinstance(content, str) else str(content), "approval": None}
        except openai.AuthenticationError:
            message = "The server's OpenAI key was rejected."
        except openai.RateLimitError:
            message = "OpenAI rate limit or quota reached."
        except GraphRecursionError:
            message = f"Stopped: too many tool steps (limit {core.MAX_STEPS})."
        except Exception as e:
            log.exception("Turn failed")
            message = f"Something went wrong ({type(e).__name__})."
        s["thread"] += 1  # history may end mid-tool-call; start clean
        return {"session_id": session_id, "reply": f"{message} The conversation was reset.", "approval": None}

    async def pending(session_id: str, s: dict) -> bool:
        state = await app.state.graph.aget_state(config_for(session_id, s))
        return bool(state.next)

    # --- routes -----------------------------------------------------------
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "demo": DEMO_MODE}

    @app.get("/api/check", dependencies=[Depends(require_password)])
    async def check() -> dict:
        return {"status": "ok"}

    @app.post("/api/chat", dependencies=[Depends(require_password)])
    async def chat(body: ChatIn) -> dict:
        session_id, s = await get_session(body.session_id)
        async with s["lock"]:
            if await pending(session_id, s):
                raise HTTPException(409, "Approve or decline the pending action first.")
            rate_limit()
            return await step(session_id, s, {"messages": [HumanMessage(body.message.strip())]})

    @app.post("/api/approve", dependencies=[Depends(require_password)])
    async def approve(body: ApproveIn) -> dict:
        if body.session_id not in sessions:
            raise HTTPException(404, "Session expired. Send your message again.")
        session_id, s = await get_session(body.session_id)
        async with s["lock"]:
            if not await pending(session_id, s):
                raise HTTPException(409, "Nothing is waiting for approval.")
            rate_limit()
            return await step(session_id, s, Command(resume="yes" if body.approve else "no"))

    return app


app = create_app()
