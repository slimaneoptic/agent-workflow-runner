"""Tool registry + a tool-calling agent. Every tool call is logged; the model's final answer must pass code checks."""
import html
import json
import os
import re
import time

import requests

MODELS = ["google/gemini-3.8-flash", "google/gemini-3.1-flash-lite"]  # fallback order
ALLOWED_HOSTS = ("en.wikipedia.org",)
FAIL_FIRST_FETCH = {"armed": os.environ.get("FAIL_FIRST_FETCH") == "1"}  # demo: prove the retry path


class Cancelled(Exception):
    pass


def api_key():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("set OPENROUTER_API_KEY in the environment")
    return key


# ---------- tools ----------
def fetch_url(url):
    """Read a public page as plain text (allow-listed hosts only, size-capped)."""
    if not url.startswith("https://") or url.split("/")[2] not in ALLOWED_HOSTS:
        raise ValueError(f"host not allowed: {url}")
    if FAIL_FIRST_FETCH["armed"]:
        FAIL_FIRST_FETCH["armed"] = False
        raise TimeoutError("simulated network timeout (demo of the retry path)")
    r = requests.get(url, timeout=20, headers={"User-Agent": "workflow-demo/1.0"})
    r.raise_for_status()
    body = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"\s+", " ", text)[:9000]


TOOLS = [
    {"type": "function", "function": {"name": "fetch_url", "description": "Read a web page as text.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "submit_brief", "description": "Submit the final brief.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"},
         "summary": {"type": "string", "description": "at most 90 words"},
         "facts": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "object", "properties": {
             "text": {"type": "string"}, "source_url": {"type": "string"}}, "required": ["text", "source_url"]}}},
         "required": ["title", "summary", "facts"]}}},
]


def validate(brief, fetched):
    """Code checks the model cannot talk its way around."""
    errors = []
    if len(brief.get("summary", "").split()) > 90:
        errors.append("summary is longer than 90 words")
    facts = brief.get("facts", [])
    if not 3 <= len(facts) <= 5:
        errors.append("need 3 to 5 facts")
    for f in facts:
        if f.get("source_url") not in fetched:
            errors.append(f"fact cites a page that was not read: {f.get('source_url')}")
    return errors


# ---------- agent loop ----------
def chat(messages, log):
    key = api_key()  # a missing key fails the step with a clear message
    for model in MODELS:
        try:
            t = time.time()
            r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "messages": messages, "tools": TOOLS,
                                    "reasoning": {"effort": "low"}, "usage": {"include": True}})
            r.raise_for_status()
            data = r.json()
            cost = float(data.get("usage", {}).get("cost") or 0)
            log("llm", f"{model} {time.time() - t:.1f}s, ${cost:.4f}", cost)
            return data["choices"][0]["message"]
        except Exception as e:  # provider down or rate-limited: try the next model
            log("llm_fallback", f"{model} failed: {type(e).__name__}", 0)
    raise RuntimeError("all models failed")


def run_agent(task, urls, previous=None, feedback=None, log=print, should_cancel=lambda: False,
              fetched=None, max_turns=8):
    """Returns (brief, fetched_pages). `fetched` lets a resumed run reuse pages already read (checkpoint)."""
    fetched = dict(fetched or {})
    system = ("You write short research briefs. Read the given pages with fetch_url, then call submit_brief. "
              "Every fact must cite the URL of a page you actually read. Do not invent facts.")
    user = f"Task: {task}\nPages: {', '.join(urls)}"
    if fetched:
        user += "\nAlready read (reuse, do not fetch again): " + ", ".join(fetched)
    if previous:
        user += f"\n\nPrevious version:\n{json.dumps(previous)}\n\nReviewer feedback to apply: {feedback}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for url, text in fetched.items():
        messages.append({"role": "user", "content": f"Content of {url} (read earlier):\n{text}"})
    for _ in range(max_turns):
        if should_cancel():
            raise Cancelled()
        msg = chat(messages, log)
        messages.append(msg)
        for call in msg.get("tool_calls") or []:
            name, args = call["function"]["name"], json.loads(call["function"]["arguments"] or "{}")
            if name == "submit_brief":
                errors = validate(args, fetched)
                if not errors:
                    log("validated", f"{len(args['facts'])} facts, all cite pages that were read", 0)
                    return args, fetched
                log("validation_failed", "; ".join(errors), 0)
                result = "Rejected: " + "; ".join(errors) + ". Fix and submit again."
            else:
                t = time.time()
                text = fetch_url(args["url"])  # exceptions bubble up to the step's retry logic
                fetched[args["url"]] = text
                log("tool", f"fetch_url {args['url']} ({len(text):,} chars, {time.time() - t:.1f}s)", 0)
                result = text
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise RuntimeError("agent did not submit a valid brief within the turn limit")
