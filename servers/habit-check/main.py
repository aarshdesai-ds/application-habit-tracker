"""Habit Check - a local MCP server (stdio).

Data stays on this machine in %APPDATA%\\habit-check\\habits.db.
Nothing is ever deleted: undo_check_in marks a day 'undone' and keeps the row.
Write tools (check_in, undo_check_in) must be approved in the client before they run.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import aiosqlite
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging: stderr only. Over stdio, stdout carries the MCP protocol.
# ---------------------------------------------------------------------------
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[habit-check] %(message)s")
log = logging.getLogger("habit-check")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Where the database lives: C:\Users\<you>\AppData\Roaming\habit-check\habits.db
# Outside the repo and outside OneDrive, so it can't be committed or hit sync locks.
_appdata = os.environ.get("APPDATA")
if not _appdata:
    raise RuntimeError("APPDATA is not set; cannot decide where to store habits.db.")

APP_DIR = Path(_appdata) / "habit-check"
DB_PATH = APP_DIR / "habits.db"

# Edit this tuple to change your habits (see README "Changing habits").
# If you change it, also update the habit list in the tool docstrings below.
HABITS = ("leetcode", "apply", "gym", "study", "sleep_by_1")
STATES = ("done", "undone")

MAX_NOTE = 300
BACKFILL_DAYS = 7  # how far back a check-in may be dated (catches typos like 2025 for 2026)

mcp = FastMCP("HabitCheck")


# ---------------------------------------------------------------------------
# Function 1: database setup (runs once at startup, safe to run again)
# ---------------------------------------------------------------------------
def init_db() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    habits_sql = ", ".join(f"'{h}'" for h in HABITS)  # built from constants so they can't drift
    states_sql = ", ".join(f"'{s}'" for s in STATES)
    con = sqlite3.connect(DB_PATH)
    try:
        with con:  # commits on success, rolls back on error
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"""
                CREATE TABLE IF NOT EXISTS checkins(
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    habit      TEXT NOT NULL CHECK (habit IN ({habits_sql})),
                    day        TEXT NOT NULL
                               CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                    state      TEXT NOT NULL CHECK (state IN ({states_sql})),
                    note       TEXT NOT NULL DEFAULT '',
                    updated_on TEXT NOT NULL,
                    UNIQUE (habit, day)
                )
            """)
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'checkins'"
            ).fetchone()
    finally:
        con.close()

    # CREATE TABLE IF NOT EXISTS never changes an existing table, so a habit added
    # to HABITS later would be rejected by the old CHECK. Fail loudly at startup instead.
    missing = [h for h in HABITS if f"'{h}'" not in row[0]]
    if missing:
        raise RuntimeError(
            f"habits.db was created before these habits were added: {', '.join(missing)}. "
            "See README 'Changing habits'."
        )
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


def _habit(value: str | None) -> str:
    # "Sleep by 1" / "sleep-by-1" -> "sleep_by_1"
    value = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value not in HABITS:
        raise InputError(f"Unknown habit '{value}'. Use one of: {', '.join(HABITS)}.")
    return value


def _day(value: str | None) -> str:
    """None or '' means today. Rejects future dates and dates older than BACKFILL_DAYS."""
    today = date.today()
    if not (value or "").strip():
        return today.isoformat()
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise InputError(f"'day' must be a date in YYYY-MM-DD format, got '{value}'.")
    if parsed > today:
        raise InputError(f"'day' ({parsed}) is in the future.")
    if parsed < today - timedelta(days=BACKFILL_DAYS):
        raise InputError(f"'day' ({parsed}) is more than {BACKFILL_DAYS} days ago.")
    return parsed.isoformat()


def _append_note(old: str, entry: str) -> str:
    return f"{old}\n{entry}" if old and entry else (old or entry)


# ---------------------------------------------------------------------------
# Function 2: resource
# ---------------------------------------------------------------------------
@mcp.resource("habits:///list", mime_type="application/json")
def habit_list() -> str:
    """The allowed habits and states, and how many days back a check-in may be dated."""
    return json.dumps({"habits": list(HABITS), "states": list(STATES), "backfill_days": BACKFILL_DAYS})


# ---------------------------------------------------------------------------
# Function 3: read tools
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
async def today() -> dict:
    """Show every habit for today and whether it is checked in. Takes no arguments."""
    try:
        day = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute("SELECT habit, state, note FROM checkins WHERE day = ?", (day,))
            found = {h: (s, n) for h, s, n in await cur.fetchall()}
        habits = [
            {"habit": h, "done": found.get(h, ("", ""))[0] == "done", "note": found.get(h, ("", ""))[1]}
            for h in HABITS
        ]
        done = sum(1 for h in habits if h["done"])
        return {"status": "success", "day": day, "done_count": done, "total": len(HABITS), "habits": habits}
    except Exception as e:
        return _error(f"Database error: {e}")


@mcp.tool(annotations={"readOnlyHint": True})
async def streaks() -> dict:
    """Current and best streak for every habit. Takes no arguments.

    current: consecutive 'done' days ending today. If today is not checked in yet,
    the streak counts back from yesterday (it is still alive until the day ends).
    best: longest run ever. total_done: all 'done' days.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute("SELECT habit, day FROM checkins WHERE state = 'done'")
            rows = await cur.fetchall()

        done_days: dict[str, set[date]] = {h: set() for h in HABITS}
        for habit, day in rows:
            if habit in done_days:  # ignore habits removed from HABITS
                done_days[habit].add(date.fromisoformat(day))

        today_ = date.today()
        one = timedelta(days=1)
        result = []
        for habit in HABITS:
            days = done_days[habit]

            current = 0
            d = today_ if today_ in days else today_ - one
            while d in days:
                current += 1
                d -= one

            best = run = 0
            prev: date | None = None
            for d in sorted(days):
                run = run + 1 if prev is not None and d - prev == one else 1
                best = max(best, run)
                prev = d

            result.append({
                "habit": habit,
                "current": current,
                "best": best,
                "total_done": len(days),
                "done_today": today_ in days,
            })
        return {"status": "success", "day": today_.isoformat(), "streaks": result}
    except Exception as e:
        return _error(f"Database error: {e}")


# ---------------------------------------------------------------------------
# Function 4: write tools (client must get the user's yes before calling)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def check_in(habit: str, day: str | None = None, note: str = "") -> dict:
    """Mark one habit as done for one day. WRITES to the database.

    habit: one of leetcode, apply, gym, study, sleep_by_1 (see habits:///list).
    day: optional YYYY-MM-DD; omit for today. Not in the future, at most 7 days ago.
    note: optional, max 300 chars; appended with today's date (old notes are kept).
    One habit per call: to check off two habits, call this twice.
    If the habit is already done for that day, nothing is written and a
    duplicate_warning is returned.
    """
    try:
        habit = _habit(habit)
        day = _day(day)
        note = _text(note, "note", MAX_NOTE)
        stamp = date.today().isoformat()
        entry = f"[{stamp}] {note}" if note else ""

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "SELECT id, state, note FROM checkins WHERE habit = ? AND day = ?", (habit, day)
            )
            row = await cur.fetchone()
            if row is not None and row[1] == "done":
                return {
                    "status": "duplicate_warning",
                    "message": f"'{habit}' is already checked in for {day} (id {row[0]}). Nothing was written.",
                }

            if row is None:
                cur = await c.execute(
                    "INSERT INTO checkins(habit, day, state, note, updated_on) VALUES (?, ?, 'done', ?, ?)",
                    (habit, day, entry, stamp),
                )
                row_id, old_state = cur.lastrowid, None
            else:  # re-checking a day that was undone
                row_id, old_state, old_note = row
                await c.execute(
                    "UPDATE checkins SET state = 'done', note = ?, updated_on = ? WHERE id = ?",
                    (_append_note(old_note, entry), stamp, row_id),
                )
            await c.commit()
        return {
            "status": "success",
            "id": row_id,
            "habit": habit,
            "day": day,
            "old_state": old_state,
            "new_state": "done",
        }
    except InputError as e:
        return _error(str(e))
    except sqlite3.IntegrityError as e:
        return _error(f"The database refused this check-in: {e}")
    except Exception as e:
        return _error(f"Database error: {e}")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def undo_check_in(habit: str, day: str | None = None, reason: str = "") -> dict:
    """Mark a checked-in habit as NOT done for one day. WRITES to the database.

    Use this to fix a mistaken check-in. The row is kept with state 'undone'
    (nothing is deleted) and the reason is appended to its note with today's date.
    habit: one of leetcode, apply, gym, study, sleep_by_1 (see habits:///list).
    day: optional YYYY-MM-DD; omit for today. Not in the future, at most 7 days ago.
    reason: optional, max 300 chars.
    """
    try:
        habit = _habit(habit)
        day = _day(day)
        reason = _text(reason, "reason", MAX_NOTE)
        stamp = date.today().isoformat()

        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "SELECT id, state, note FROM checkins WHERE habit = ? AND day = ?", (habit, day)
            )
            row = await cur.fetchone()
            if row is None:
                return _error(f"'{habit}' has no check-in on {day}, so there is nothing to undo.")
            row_id, old_state, old_note = row
            if old_state == "undone":
                return {
                    "status": "duplicate_warning",
                    "message": f"'{habit}' on {day} is already undone (id {row_id}). Nothing was written.",
                }

            entry = f"[{stamp}] undone" + (f": {reason}" if reason else "")
            await c.execute(
                "UPDATE checkins SET state = 'undone', note = ?, updated_on = ? WHERE id = ?",
                (_append_note(old_note, entry), stamp, row_id),
            )
            await c.commit()
        return {
            "status": "success",
            "id": row_id,
            "habit": habit,
            "day": day,
            "old_state": old_state,
            "new_state": "undone",
        }
    except InputError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Database error: {e}")


# ---------------------------------------------------------------------------
# Start (stdio)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
