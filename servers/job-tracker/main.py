"""Job Application Tracker - a local MCP server (stdio).

Data stays on this machine in %APPDATA%\\job-tracker\\jobs.db.
Nothing is ever deleted: use status "withdrawn" instead.
Write tools (add_application, update_status) must be approved in the client
(LangGraph interrupt() or the chat app's tool-approval prompt) before they run.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import aiosqlite
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging: stderr only. Over stdio, stdout carries the MCP protocol.
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[job-tracker] %(message)s")
log = logging.getLogger("job-tracker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Where the database lives: C:\Users\<you>\AppData\Roaming\job-tracker\jobs.db
# Outside the repo and outside OneDrive, so it can't be committed or hit sync locks.
_appdata = os.environ.get("APPDATA")
if not _appdata:
    raise RuntimeError("APPDATA is not set; cannot decide where to store jobs.db.")

APP_DIR = Path(_appdata) / "job-tracker"
DB_PATH = APP_DIR / "jobs.db"

STATUSES = ("applied", "oa", "interview", "offer", "rejected", "withdrawn")

MAX_COMPANY = 100
MAX_ROLE = 100
MAX_RESUME = 100
MAX_LINK = 500
MAX_NOTE = 1000
DUPLICATE_WINDOW_DAYS = 7

mcp = FastMCP("JobTracker")


# ---------------------------------------------------------------------------
# Function 1: database setup (runs once at startup, safe to run again)
# ---------------------------------------------------------------------------
def init_db() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    allowed = ", ".join(f"'{s}'" for s in STATUSES)  # built from STATUSES so they can't drift
    with sqlite3.connect(DB_PATH) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS applications(
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                company    TEXT NOT NULL,
                role       TEXT NOT NULL,
                applied_on TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ({allowed})),
                resume     TEXT NOT NULL DEFAULT '',
                link       TEXT NOT NULL DEFAULT '',
                note       TEXT NOT NULL DEFAULT '',
                updated_on TEXT NOT NULL
            )
        """)
    log.info("Database ready at %s", DB_PATH)


init_db()


# ---------------------------------------------------------------------------
# Validation helpers (checks done in code, not trusted to the model)
# ---------------------------------------------------------------------------
class InputError(ValueError):
    """Bad input from the caller; message is safe to return to the model."""


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


def _text(value: str | None, field: str, max_len: int, required: bool = False) -> str:
    value = (value or "").strip()
    if required and not value:
        raise InputError(f"'{field}' is required.")
    if len(value) > max_len:
        raise InputError(f"'{field}' is too long ({len(value)} chars, max {max_len}).")
    return value


def _status(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in STATUSES:
        raise InputError(f"Unknown status '{value}'. Use one of: {', '.join(STATUSES)}.")
    return value


def _date(value: str, field: str) -> str:
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        raise InputError(f"'{field}' must be a date in YYYY-MM-DD format, got '{value}'.")
    if parsed > date.today():
        raise InputError(f"'{field}' ({parsed}) is in the future.")
    return parsed.isoformat()


def _link(value: str | None) -> str:
    value = _text(value, "link", MAX_LINK)
    if value and not value.lower().startswith(("http://", "https://")):
        raise InputError("'link' must start with http:// or https://.")
    return value


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(cur: aiosqlite.Cursor, rows) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Function 2: resource
# ---------------------------------------------------------------------------
@mcp.resource("jobs:///statuses", mime_type="application/json")
def statuses() -> str:
    """The allowed application statuses, in pipeline order."""
    return json.dumps({"statuses": list(STATUSES)})


# ---------------------------------------------------------------------------
# Function 3: read tools
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def list_applications(
    status: str | None = None,
    company: str | None = None,
    limit: int = 20,
) -> dict:
    """List job applications, newest first.

    status: optional filter, one of applied, oa, interview, offer, rejected, withdrawn.
    company: optional, case-insensitive partial match (e.g. "stripe" finds "Stripe").
    limit: max rows to return, 1-100.
    """
    try:
        limit = max(1, min(int(limit), 100))
        query = """
            SELECT id, company, role, applied_on, status, resume, link, note, updated_on
            FROM applications WHERE 1=1
        """
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(_status(status))
        if company:
            query += " AND company LIKE ? ESCAPE '\\'"
            params.append(f"%{_like_escape(_text(company, 'company', MAX_COMPANY))}%")
        query += " ORDER BY applied_on DESC, id DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(query, params)
            rows = _rows(cur, await cur.fetchall())
        return {"status": "success", "count": len(rows), "applications": rows}
    except InputError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Database error: {e}")


@mcp.tool(annotations={"readOnlyHint": True})
async def followups_due(days: int = 10) -> dict:
    """Applications still at status 'applied' after at least `days` days (1-90), oldest first."""
    try:
        days = max(1, min(int(days), 90))
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT id, company, role, applied_on, resume, link
                FROM applications
                WHERE status = 'applied' AND applied_on <= ?
                ORDER BY applied_on ASC, id ASC
                """,
                (cutoff,),
            )
            rows = _rows(cur, await cur.fetchall())
        today = date.today()
        for r in rows:
            r["days_waiting"] = (today - date.fromisoformat(r["applied_on"])).days
        return {"status": "success", "count": len(rows), "followups": rows}
    except Exception as e:
        return _error(f"Database error: {e}")


# ---------------------------------------------------------------------------
# Function 4: write tools (client must get the user's yes before calling)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def add_application(
    company: str,
    role: str,
    applied_on: str,
    resume: str = "",
    link: str = "",
    note: str = "",
    allow_duplicate: bool = False,
) -> dict:
    """Log a new job application with status 'applied'. WRITES to the database.

    applied_on: YYYY-MM-DD, not in the future.
    resume: which resume version was sent (e.g. "resume_v3").
    link: optional job posting URL (http/https).
    allow_duplicate: set True only if the user confirms a repeat of the same
    company + role within 7 days is intended.
    """
    try:
        company = _text(company, "company", MAX_COMPANY, required=True)
        role = _text(role, "role", MAX_ROLE, required=True)
        applied_on = _date(applied_on, "applied_on")
        resume = _text(resume, "resume", MAX_RESUME)
        link = _link(link)
        note = _text(note, "note", MAX_NOTE)
        today = date.today().isoformat()

        async with aiosqlite.connect(DB_PATH) as c:
            if not allow_duplicate:
                since = (date.fromisoformat(applied_on) - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
                cur = await c.execute(
                    """
                    SELECT id, applied_on FROM applications
                    WHERE company = ? COLLATE NOCASE AND role = ? COLLATE NOCASE
                      AND applied_on >= ?
                    """,
                    (company, role, since),
                )
                dup = await cur.fetchone()
                if dup:
                    return {
                        "status": "duplicate_warning",
                        "message": (
                            f"'{company} - {role}' was already logged as id {dup[0]} on {dup[1]}. "
                            "Nothing was written. Ask the user; call again with allow_duplicate=True only if they confirm."
                        ),
                    }

            cur = await c.execute(
                """
                INSERT INTO applications(company, role, applied_on, status, resume, link, note, updated_on)
                VALUES (?, ?, ?, 'applied', ?, ?, ?, ?)
                """,
                (company, role, applied_on, resume, link, note, today),
            )
            new_id = cur.lastrowid
            await c.commit()
        return {"status": "success", "id": new_id, "message": f"Logged {company} - {role} ({applied_on})."}
    except InputError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Database error: {e}")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def update_status(id: int, status: str, note: str = "") -> dict:
    """Change an application's status. WRITES to the database.

    status: one of applied, oa, interview, offer, rejected, withdrawn.
    note: optional; appended to the existing note with today's date (old notes are kept).
    There is no delete: use status 'withdrawn' instead.
    """
    try:
        app_id = int(id)
        new_status = _status(status)
        note = _text(note, "note", MAX_NOTE)
        today = date.today().isoformat()

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "SELECT company, role, status, note FROM applications WHERE id = ?", (app_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return _error(f"No application with id {app_id}. Use list_applications to find the id.")
            company, role, old_status, old_note = row

            new_note = old_note
            if note:
                entry = f"[{today}] {note}"
                new_note = f"{old_note}\n{entry}" if old_note else entry

            await c.execute(
                "UPDATE applications SET status = ?, note = ?, updated_on = ? WHERE id = ?",
                (new_status, new_note, today, app_id),
            )
            await c.commit()
        return {
            "status": "success",
            "id": app_id,
            "company": company,
            "role": role,
            "old_status": old_status,
            "new_status": new_status,
        }
    except InputError as e:
        return _error(str(e))
    except (TypeError, ValueError):
        return _error(f"'id' must be a whole number, got '{id}'.")
    except Exception as e:
        return _error(f"Database error: {e}")


# ---------------------------------------------------------------------------
# Start (stdio)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
