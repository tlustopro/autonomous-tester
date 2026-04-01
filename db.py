"""
SQLite persistence — runs, steps, screenshots.

Schema:
  runs   (id, scenario, base_url, status, summary, created_at, finished_at)
  steps  (id, run_id, seq, tool, input_json, result, is_pass, is_fail, screenshot_path)
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.getenv("DATABASE_PATH", "/tmp/qa.db")

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn():
    with _lock:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario    TEXT    NOT NULL,
            base_url    TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'running',
            passed      INTEGER,
            summary     TEXT,
            failures    TEXT,           -- JSON array
            created_at  TEXT    NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS steps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES runs(id),
            seq             INTEGER NOT NULL,
            tool            TEXT    NOT NULL,
            input_json      TEXT,
            result          TEXT,
            is_pass         INTEGER NOT NULL DEFAULT 0,
            is_fail         INTEGER NOT NULL DEFAULT 0,
            screenshot_path TEXT,
            created_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
        """)


# ── Runs ────────────────────────────────────────────────────────────────────

def create_run(scenario: str, base_url: str) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO runs (scenario, base_url, status, created_at) VALUES (?,?,?,?)",
            (scenario, base_url, "running", _now()),
        )
        return cur.lastrowid


def finish_run(run_id: int, passed: bool, summary: str, failures: list[str]):
    with _conn() as con:
        con.execute(
            """UPDATE runs SET status=?, passed=?, summary=?, failures=?, finished_at=?
               WHERE id=?""",
            (
                "passed" if passed else "failed",
                1 if passed else 0,
                summary,
                json.dumps(failures),
                _now(),
                run_id,
            ),
        )


def fail_run(run_id: int, error: str):
    with _conn() as con:
        con.execute(
            "UPDATE runs SET status='error', summary=?, finished_at=? WHERE id=?",
            (error, _now(), run_id),
        )


def list_runs(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_steps(run_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Steps ────────────────────────────────────────────────────────────────────

def add_step(
    run_id: int,
    seq: int,
    tool: str,
    input_data: dict,
    result: str,
    screenshot_path: str | None = None,
) -> int:
    is_pass = 1 if result.startswith("PASS") else 0
    is_fail = 1 if (result.startswith("FAIL") or result.startswith("ERROR")) else 0
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO steps
               (run_id, seq, tool, input_json, result, is_pass, is_fail, screenshot_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id, seq, tool,
                json.dumps(input_data),
                result,
                is_pass, is_fail,
                screenshot_path,
                _now(),
            ),
        )
        return cur.lastrowid
