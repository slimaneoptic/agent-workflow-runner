"""Dashboard: submit tasks, watch runs live, review outputs (approve or request changes), cancel."""
import html
import json
import time

import markdown
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

import db

app = FastAPI(title="Agent workflow runner")
BADGE = {"done": "#1d7a3a", "queued": "#8a94a0", "running": "#2A78D6", "needs_review": "#b7791f", "approved": "#1d7a3a",
         "changes_requested": "#6b4fb3", "failed": "#b0301f", "cancelled": "#5B6B7A"}
CSS = """body{margin:0;font-family:system-ui,sans-serif;background:#F6F8FA;color:#14202B}
main{max-width:1020px;margin:0 auto;padding:1.2rem 1rem 3rem} h1{font-size:1.5rem;margin:.2rem 0 1rem}
h2{font-size:1.05rem;margin:1.6rem 0 .5rem} table{border-collapse:collapse;width:100%;background:#fff;font-size:.9rem}
td,th{padding:.45rem .6rem;border-bottom:1px solid #D5DBE1;text-align:left;vertical-align:top} th{color:#5B6B7A}
.b{color:#fff;border-radius:999px;padding:.12rem .55rem;font-size:.8rem;white-space:nowrap}
.card{background:#fff;border:1px solid #D5DBE1;border-radius:8px;padding:.9rem 1rem}
input,textarea{width:100%;box-sizing:border-box;font:inherit;padding:.45rem;border:1px solid #D5DBE1;border-radius:6px}
button{font:inherit;padding:.45rem .9rem;border:0;border-radius:6px;background:#14202B;color:#fff;cursor:pointer}
button.alt{background:#fff;color:#14202B;border:1px solid #14202B} .muted{color:#5B6B7A;font-size:.85rem}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem} a{color:#2A78D6}"""


def badge(s):
    return f"<span class='b' style='background:{BADGE.get(s, '#999')}'>{s.replace('_', ' ')}</span>"


def page(title, body, refresh=False):
    meta = "<meta http-equiv='refresh' content='2'>" if refresh else ""
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'>{meta}<title>{title}</title>"
                        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>")


@app.get("/")
def index():
    c = db.conn()
    rows = c.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    trs = "".join(f"<tr><td><a href='/jobs/{r['id']}'>#{r['id']}</a></td><td>{html.escape(r['title'])}</td>"
                  f"<td>v{r['version']}</td><td>{badge(r['status'])}</td><td>${r['cost']:.4f}</td>"
                  f"<td class='muted'>{time.strftime('%H:%M:%S', time.localtime(r['updated']))}</td></tr>" for r in rows)
    live = any(r["status"] in ("queued", "running") for r in rows)
    return page("Workflow runner", f"""<h1>Agent workflow runner</h1>
<div class='grid'><div class='card'><h2 style='margin-top:0'>New task</h2>
<form method='post' action='/jobs'><p><input name='title' placeholder='Title' required></p>
<p><textarea name='task' rows='3' placeholder='What should the agent produce?' required></textarea></p>
<p><textarea name='urls' rows='2' placeholder='Source pages, one per line'></textarea></p><button>Submit</button></form></div>
<div class='card'><h2 style='margin-top:0'>How a run works</h2><p class='muted'>Queued, then a worker runs checkpointed steps
(research, draft, publish) with retries and backoff; a crashed worker's job resumes from its last checkpoint.
The agent's final answer must pass code checks. Output waits for human review: approve, or request changes,
which starts the next version with your comment.</p></div></div>
<h2>Jobs</h2><table><tr><th>Job</th><th>Title</th><th>Version</th><th>Status</th><th>Cost</th><th>Updated</th></tr>{trs}</table>""", live)


@app.post("/jobs")
def submit(title: str = Form(...), task: str = Form(...), urls: str = Form("")):
    c = db.conn()
    db.create_job(c, title, task, [u.strip() for u in urls.splitlines() if u.strip()])
    return RedirectResponse("/", 303)


@app.get("/jobs/{jid}")
def detail(jid: int):
    c = db.conn()
    j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    steps = c.execute("SELECT * FROM steps WHERE job_id=? ORDER BY started", (jid,)).fetchall()
    events = c.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    t0 = events[0]["ts"] if events else 0
    step_rows = "".join(f"<tr><td>{s['name']}</td><td>{badge('done' if s['status'] == 'done' else 'running')}"
                        f"</td><td>{s['attempts']}</td><td>{(s['ended'] or time.time()) - s['started']:.1f}s</td></tr>"
                        for s in steps)
    ev_rows = "".join(f"<tr><td class='muted'>+{e['ts'] - t0:.1f}s</td><td><b>{e['kind']}</b></td>"
                      f"<td>{html.escape(e['detail'] or '')}</td></tr>" for e in events)
    out = ""
    if j["output"]:
        b = json.loads(j["output"])
        facts = "".join(f"<li>{html.escape(f['text'])} <a href='{f['source_url']}'>source</a></li>" for f in b["facts"])
        out = f"<div class='card'><h2 style='margin-top:0'>{html.escape(b['title'])}</h2><p>{html.escape(b['summary'])}</p><ul>{facts}</ul></div>"
    actions = ""
    if j["status"] == "needs_review":
        actions = f"""<form method='post' action='/jobs/{jid}/review' class='card'><h2 style='margin-top:0'>Review</h2>
<p><textarea name='comment' rows='2' placeholder='What should change? (only for "request changes")'></textarea></p>
<button name='action' value='approve'>Approve</button> <button class='alt' name='action' value='changes'>Request changes</button></form>"""
    elif j["status"] in ("queued", "running"):
        actions = f"<form method='post' action='/jobs/{jid}/cancel'><button class='alt'>Cancel run</button></form>"
    lineage = f" · follows <a href='/jobs/{j['parent_id']}'>#{j['parent_id']}</a>" if j["parent_id"] else ""
    fb = f"<p class='muted'>Reviewer feedback applied: “{html.escape(j['feedback'])}”</p>" if j["feedback"] else ""
    return page(f"Job {jid}", f"""<p><a href='/'>← all jobs</a></p>
<h1>#{jid} {html.escape(j['title'])} {badge(j['status'])}</h1>
<p class='muted'>Version {j['version']}{lineage} · cost ${j['cost']:.4f} · task: {html.escape(j['task'])}</p>{fb}
{out}{actions}
<h2>Steps (checkpoints)</h2><table><tr><th>Step</th><th>State</th><th>Attempts</th><th>Time</th></tr>{step_rows}</table>
<h2>Event log</h2><table>{ev_rows}</table>""", j["status"] in ("queued", "running"))


@app.post("/jobs/{jid}/cancel")
def cancel(jid: int):
    c = db.conn()
    c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
    db.event(c, jid, "cancel_requested", "by user")
    if c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "queued":
        db.set_status(c, jid, "cancelled")
    return RedirectResponse(f"/jobs/{jid}", 303)


@app.post("/jobs/{jid}/review")
def review(jid: int, action: str = Form(...), comment: str = Form("")):
    c = db.conn()
    j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if action == "approve":
        db.set_status(c, jid, "approved")
        return RedirectResponse(f"/jobs/{jid}", 303)
    db.set_status(c, jid, "changes_requested")
    new = db.create_job(c, j["title"], j["task"], json.loads(j["urls"]), parent_id=jid,
                        feedback=comment or "Improve the brief.", version=j["version"] + 1)
    return RedirectResponse(f"/jobs/{new}", 303)
