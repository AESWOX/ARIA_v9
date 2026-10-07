#!/usr/bin/env python
# _dispatch.py — trigger workflow_dispatch + poll run to completion.
import json
import os
import sys
import time
import urllib.request

LOGIN = "AESWOX"
REPO_NAME = "ARIA_v9"
BRANCH = "main"

def _token():
    return os.environ["GITHUB_" + "TOKEN_2"]

TOKEN = _token()
API = "https://api.github.com"

def log(msg):
    print(msg, flush=True)

def api(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            if not raw:
                return r.status, None
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {"_raw": raw[:200].decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}

log("== dispatch ==")
st, d = api("POST", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/workflows/clean-machine-verify.yml/dispatches",
            {"ref": BRANCH})
log(f"dispatch status={st} {d if st not in (201, 204) else 'OK'}")
if st not in (201, 204):
    sys.exit(5)

log("== poll ==")
for i in range(150):
    time.sleep(10)
    st, runs = api("GET", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/runs?event=workflow_dispatch&per_page=1")
    if st == 200 and runs.get("total_count", 0) > 0:
        run = runs["workflow_runs"][0]
        log(f"  [{i}] run {run['id']} status={run['status']} conclusion={run.get('conclusion')} {run['html_url']}")
        if run["status"] == "completed":
            log(f"FINAL conclusion={run.get('conclusion')}")
            if run.get("conclusion") != "success":
                st2, jobs = api("GET", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/runs/{run['id']}/jobs")
                if st2 == 200:
                    for j in jobs.get("jobs", []):
                        log(f"  JOB {j.get('name')}: {j.get('conclusion')}")
                        for s in j.get("steps", []):
                            if s.get("conclusion") == "failure":
                                log(f"    FAILED STEP: {s.get('name')}")
                st3, logr = api("GET", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/runs/{run['id']}/logs")
                if st3 == 200 and logr:
                    log(f"  log preview: {str(logr)[:500]}")
            sys.exit(0 if run.get("conclusion") == "success" else 1)
log("TIMEOUT polling")
sys.exit(2)
