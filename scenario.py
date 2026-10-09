"""Replay the whole run for real: retry after a network error, crash + resume from checkpoint,
review with requested changes (v2), approval, and cancellation. Screenshots go to outputs/."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

PY = sys.executable
BASE = "http://127.0.0.1:8791"
SHOTS = Path("outputs")
URLS = "https://en.wikipedia.org/wiki/Capacity_factor\nhttps://en.wikipedia.org/wiki/Wind_power"


def worker(env=None, wait=True):
    e = dict(os.environ, **(env or {}))
    p = subprocess.Popen([PY, "worker.py"] + (["--once"] if wait else []), env=e)
    if wait:
        p.wait()
    return p


def status(jid):
    import sqlite3
    return sqlite3.connect("data/runner.db").execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]


def shot(path, name, height=1500):
    subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1200,%d" % height,
                    f"--screenshot={SHOTS / name}", BASE + path], capture_output=True)


def main():
    shutil.rmtree("data", ignore_errors=True)
    SHOTS.mkdir(exist_ok=True)
    server = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--port", "8791", "--log-level", "warning"])
    for _ in range(50):  # wait until the server answers
        try:
            requests.get(BASE, timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.2)
    try:
        s = requests.Session()
        s.post(BASE + "/jobs", data={"title": "Wind vs solar: capacity factor brief",
                                     "task": "Explain capacity factor and how wind power compares, for a non-technical manager.",
                                     "urls": URLS})
        # 1. worker hits a network error (retried), finishes 'research', then crashes
        worker({"FAIL_FIRST_FETCH": "1", "CRASH_AFTER": "research"})
        print("after crash:", status(1))
        time.sleep(22)  # heartbeat goes stale
        # 2. a new worker resumes from the checkpoint and finishes
        worker()
        print("after resume:", status(1))
        shot("/jobs/1", "1_retry_crash_resume.png", 1900)
        # 3. reviewer requests changes -> v2 runs with the feedback and the previous output as context
        r = s.post(BASE + "/jobs/1/review", data={"action": "changes",
                   "comment": "Keep the summary under 50 words and add one fact about offshore wind."})
        worker()
        v2 = int(r.url.rsplit("/", 1)[1])
        print("v2:", v2, status(v2))
        s.post(BASE + f"/jobs/{v2}/review", data={"action": "approve"})
        shot(f"/jobs/{v2}", "2_changes_applied_approved.png", 1500)
        # 4. a run cancelled by the user while the worker is busy
        s.post(BASE + "/jobs", data={"title": "Cancelled run", "task": "Summarise the page.", "urls": URLS})
        bg = worker(wait=False)
        cid = v2 + 1
        for _ in range(60):
            if status(cid) == "running":
                break
            time.sleep(0.2)
        time.sleep(1.5)
        s.post(BASE + f"/jobs/{cid}/cancel")
        for _ in range(60):
            if status(cid) in ("cancelled", "needs_review", "failed"):
                break
            time.sleep(0.5)
        bg.terminate()
        print("cancel run:", status(cid))
        shot(f"/jobs/{cid}", "3_cancelled.png", 1000)
        shot("/", "0_dashboard.png", 900)
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
