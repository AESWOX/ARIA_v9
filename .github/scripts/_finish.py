#!/usr/bin/env python
# _finish.py — push (force ok: empty repo), scrub token, dispatch, poll.
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = r"C:\Users\User\Desktop\ARIA_v9"
LOGIN = "AESWOX"
REPO_NAME = "ARIA_v9"
BRANCH = "main"
TOKEN = os.environ["GITHUB_TOKEN_2"]
API = "https://api.github.com"

def log(msg):
    print(msg, flush=True)

def sh(cmd, timeout=600):
    log(f"  $ {' '.join(cmd[:3])} ...")
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace")[-1500:]
        err = (e.stderr or b"").decode(errors="replace")[-1500:]
        log(f"TIMEOUT: {out}\n{err}")
        return None
    if r.returncode != 0:
        log(f"  rc={r.returncode} err={r.stderr[-800:]}")
    return r

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
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}

log("== 1) push ==")
r = sh(["git", "push", "-u", "origin", BRANCH, "--force"], timeout=600)
if r is None or r.returncode != 0:
    log("FATAL push failed")
    sys.exit(4)
log("push OK")

log("== 2) scrub token from remote ==")
sh(["git", "remote", "set-url", "origin", f"https://github.com/{LOGIN}/{REPO_NAME}.git"], timeout=30)
log("remote scrubbed")

log("== 3) dispatch ==")
st, d = api("POST", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/workflows/clean-machine-verify.yml/dispatches",
            {"ref": BRANCH})
log(f"dispatch status={st} {d if st not in (201, 204) else 'OK'}")
if st not in (201, 204):
    sys.exit(5)

log("== 4) poll ==")
for i in range(120):
    time.sleep(10)
    st, runs = api("GET", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/runs?event=workflow_dispatch&per_page=1")
    if st == 200 and runs.get("total_count", 0) > 0:
        run = runs["workflow_runs"][0]
        log(f"  [{i}] run {run['id']} status={run['status']} conclusion={run.get('conclusion')} {run['html_url']}")
        if run["status"] == "completed":
            log(f"FINAL conclusion={run.get('conclusion')}")
            # fetch failed step log info
            if run.get("conclusion") != "success":
                st2, jobs = api("GET", f"{API}/repos/{LOGIN}/{REPO_NAME}/actions/runs/{run['id']}/jobs")
                if st2 == 200:
                    for j in jobs.get("jobs", []):
                        for s in j.get("steps", []):
                            if s.get("conclusion") == "failure":
                                log(f"  FAILED STEP: {s.get('name')}")
            sys.exit(0 if run.get("conclusion") == "success" else 1)
log("TIMEOUT polling")
sys.exit(2)
