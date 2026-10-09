"""Background worker: claims jobs, runs them as checkpointed steps with retries, honours cancellation.

Steps: research (agent reads pages; pages are checkpointed) -> draft (agent submits a brief that code validates)
-> publish (artifact saved, job goes to human review). A crashed worker's job is picked up again by the next
worker (stale heartbeat) and resumes from the last completed step.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import db
from agent import Cancelled, run_agent

CRASH_AFTER = os.environ.get("CRASH_AFTER")  # demo: kill the worker after this step to prove resume
MAX_ATTEMPTS, BACKOFF = 3, 2.0


def heartbeat(job_id, stop):
    c = db.conn()
    while not stop.is_set():
        c.execute("UPDATE jobs SET heartbeat=? WHERE id=?", (time.time(), job_id))
        stop.wait(3)


def run_step(c, job, name, fn):
    done = db.step_done(c, job["id"], name)
    if done is not None:
        db.event(c, job["id"], "step_skipped", f"{name}: already done before the restart, reusing its checkpoint")
        return done
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]:
            raise Cancelled()
        c.execute("INSERT OR REPLACE INTO steps (job_id, name, status, attempts, started) VALUES (?,?,?,?,?)",
                  (job["id"], name, "running", attempt, time.time()))
        db.event(c, job["id"], "step_started", f"{name} (attempt {attempt})")
        try:
            out = fn()
        except Cancelled:
            raise
        except Exception as e:
            db.event(c, job["id"], "step_error", f"{name}: {type(e).__name__}: {e}")
            if attempt == MAX_ATTEMPTS:
                raise
            wait = BACKOFF ** attempt
            db.event(c, job["id"], "retry", f"{name}: retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        c.execute("UPDATE steps SET status='done', output=?, ended=? WHERE job_id=? AND name=?",
                  (json.dumps(out), time.time(), job["id"], name))
        db.event(c, job["id"], "step_done", name)
        if CRASH_AFTER == name:
            db.event(c, job["id"], "worker_crash", f"worker killed after '{name}' (simulated crash)")
            os._exit(1)
        return out


def process(c, job):
    jid = job["id"]
    urls = json.loads(job["urls"])
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(jid, stop), daemon=True).start()

    def log(kind, detail, cost):
        db.event(c, jid, kind, detail)
        if cost:
            c.execute("UPDATE jobs SET cost = cost + ? WHERE id=?", (cost, jid))

    cancel = lambda: bool(c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()[0])
    previous = None
    if job["parent_id"]:
        p = c.execute("SELECT output FROM jobs WHERE id=?", (job["parent_id"],)).fetchone()
        previous = json.loads(p["output"]) if p and p["output"] else None
    try:
        pages = run_step(c, job, "research", lambda: read_pages(urls, log, cancel))
        brief = run_step(c, job, "draft", lambda: run_agent(job["task"], urls, previous, job["feedback"], log=log,
                                                            should_cancel=cancel, fetched=pages)[0])
        run_step(c, job, "publish", lambda: publish(jid, job["version"], brief))
        db.set_status(c, jid, "needs_review", json.dumps(brief))
    except Cancelled:
        db.set_status(c, jid, "cancelled")
    except Exception as e:
        db.event(c, jid, "failed", f"{type(e).__name__}: {e}")
        db.set_status(c, jid, "failed")
    finally:
        stop.set()


def read_pages(urls, log, cancel):
    """Research step: read every page once; the result is checkpointed so a restart never re-reads it."""
    from agent import fetch_url
    pages = {}
    for u in urls:
        if cancel():
            raise Cancelled()
        t = time.time()
        pages[u] = fetch_url(u)
        log("tool", f"fetch_url {u} ({len(pages[u]):,} chars, {time.time() - t:.1f}s)", 0)
    return pages


def publish(jid, version, brief):
    path = Path(f"data/artifacts/job{jid}_v{version}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    md = f"# {brief['title']}\n\n{brief['summary']}\n\n" + "\n".join(
        f"- {f['text']} ([source]({f['source_url']}))" for f in brief["facts"])
    path.write_text(md)
    return {"artifact": str(path)}


def main(once=False):
    c = db.conn()
    while True:
        job = db.claim(c)
        if job:
            if job["heartbeat"] and job["status"] == "running":
                db.event(c, job["id"], "resumed", "previous worker stopped heart-beating; resuming from checkpoints")
            process(c, job)
        elif once:
            return
        else:
            time.sleep(1)


if __name__ == "__main__":
    main(once="--once" in sys.argv)
