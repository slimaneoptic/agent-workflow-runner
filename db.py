"""Job store: jobs, steps (checkpoints) and events. SQLite here; the schema is plain SQL."""
import json
import sqlite3
import time
from pathlib import Path

DB = Path("data/runner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, title TEXT, task TEXT, urls TEXT,
    status TEXT,                -- queued | running | needs_review | approved | changes_requested | failed | cancelled
    version INTEGER DEFAULT 1, parent_id INTEGER, feedback TEXT,
    cancel_requested INTEGER DEFAULT 0, heartbeat REAL, cost REAL DEFAULT 0,
    output TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS steps (
    job_id INTEGER, name TEXT, status TEXT, attempts INTEGER DEFAULT 0, output TEXT,
    started REAL, ended REAL, PRIMARY KEY (job_id, name));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, job_id INTEGER, ts REAL, kind TEXT, detail TEXT);
"""


def conn():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB, timeout=30, isolation_level=None)  # autocommit; explicit BEGIN where needed
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def event(c, job_id, kind, detail=""):
    c.execute("INSERT INTO events (job_id, ts, kind, detail) VALUES (?,?,?,?)", (job_id, time.time(), kind, detail))


def create_job(c, title, task, urls, parent_id=None, feedback=None, version=1):
    now = time.time()
    cur = c.execute("INSERT INTO jobs (title, task, urls, status, version, parent_id, feedback, created, updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (title, task, json.dumps(urls), "queued", version, parent_id, feedback, now, now))
    event(c, cur.lastrowid, "queued", f"v{version}" + (f", feedback: {feedback}" if feedback else ""))
    return cur.lastrowid


def claim(c, stale_after=20):
    """Atomically take the oldest queued job, or a running job whose worker stopped heart-beating (crash)."""
    c.execute("BEGIN IMMEDIATE")
    row = c.execute("SELECT * FROM jobs WHERE status='queued' OR (status='running' AND heartbeat < ?) "
                    "ORDER BY created LIMIT 1", (time.time() - stale_after,)).fetchone()
    if row:
        c.execute("UPDATE jobs SET status='running', heartbeat=?, updated=? WHERE id=?",
                  (time.time(), time.time(), row["id"]))
    c.execute("COMMIT")
    return row


def set_status(c, job_id, status, output=None):
    c.execute("UPDATE jobs SET status=?, updated=?, output=COALESCE(?, output) WHERE id=?",
              (status, time.time(), output, job_id))
    event(c, job_id, status)


def step_done(c, job_id, name):
    r = c.execute("SELECT status, output FROM steps WHERE job_id=? AND name=?", (job_id, name)).fetchone()
    return json.loads(r["output"]) if r and r["status"] == "done" else None
