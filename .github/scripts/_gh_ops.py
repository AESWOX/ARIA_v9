#!/usr/bin/env python
# gh_ops.py — init git, create repo if missing, push, trigger workflow_dispatch.
# Reads token from env (never printed, never in argv). Run with python -u.
import json
import os
import subprocess
import sys
import time
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN_2"]
API = "https://api.github.com"
REPO_PATH = r"C:\Users\User\Desktop\ARIA_v9"
REPO_NAME = "ARIA_v9"
BRANCH = "main"

def log(msg):
    print(msg, flush=True)

def api(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except Exception as e:
        return 0, {"err": str(e)}

def sh(cmd, cwd=REPO_PATH, check=True, timeout=300):
    log(f"  $ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace")[-1500:]
        err = (e.stderr or b"").decode(errors="replace")[-1500:]
        raise RuntimeError(f"TIMEOUT: {' '.join(cmd)}\nstdout: {out}\nstderr: {err}")
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd {' '.join(cmd)} rc={r.returncode}\nstdout: {r.stdout[-2000:]}\nstderr: {r.stderr[-2000:]}")
    return r

def main():
    log("== 0) whoami ==")
    st, me = api("GET", f"{API}/user")
    if st != 200:
        log(f"FATAL: /user -> {st} {me}")
        sys.exit(3)
    login = me["login"]
    log(f"user: {login}")

    log("== 1) ensure repo ==")
    st, repo = api("GET", f"{API}/repos/{login}/{REPO_NAME}")
    if st == 200:
        log(f"repo exists: {repo['full_name']} private={repo['private']}")
    elif st == 404:
        st, created = api("POST", f"{API}/user/repos",
                          {"name": REPO_NAME, "private": True,
                           "description": "ARIA local agent desktop (same-origin SPA refactor, 8 DoD + 0-OPTIONS verified)"})
        if st not in (201, 200):
            log(f"FATAL: create repo -> {st} {created}")
            sys.exit(3)
        log(f"repo created: {created.get('full_name')}")
        repo = created
    else:
        log(f"FATAL: repo check -> {st} {repo}")
        sys.exit(3)

    log("== 2) git init/commit/push ==")
    sh(["git", "init", "-b", BRANCH], check=False, timeout=60)
    sh(["git", "config", "user.name", login], timeout=30)
    sh(["git", "config", "user.email", f"{login}@users.noreply.github.com"], timeout=30)
    sh(["git", "config", "http.postBuffer", "524288000"], timeout=30)
    r = sh(["git", "add", "-A"], check=False, timeout=600)
    if r.returncode != 0:
        log(f"  git add rc={r.returncode}: {r.stderr[-800:]}")
    nr = subprocess.run(["git", "diff", "--cached", "--numstat"], cwd=REPO_PATH,
                        capture_output=True, text=True, timeout=120)
    log(f"  staged files: {len(nr.stdout.strip().splitlines()) if nr.stdout.strip() else 0}")
    r2 = sh(["git", "commit", "-m", "ARIA_v9: same-origin SPA refactor (8 DoD + 0-OPTIONS verified)"], check=False, timeout=120)
    if r2.returncode != 0 and "nothing to commit" not in r2.stdout and "nothing to commit" not in r2.stderr:
        log(f"  commit rc={r2.returncode}: {r2.stdout[-400:]} {r2.stderr[-400:]}")

    push_url = f"https://{login}:{TOKEN}@github.com/{login}/{REPO_NAME}.git"
    sh(["git", "remote", "remove", "origin"], check=False, timeout=30)
    sh(["git", "remote", "add", "origin", push_url], timeout=30)
    r5 = sh(["git", "push", "-u", "origin", BRANCH, "--force"], check=False, timeout=600)
    if r5.returncode != 0:
        log(f"FATAL: push rc={r5.returncode}\nstdout: {r5.stdout[-2000:]}\nstderr: {r5.stderr[-2000:]}")
        sys.exit(4)
    log("push OK")
    # scrub token from remote URL now that push is done
    sh(["git", "remote", "set-url", "origin", f"https://github.com/{login}/{REPO_NAME}.git"], timeout=30)
    log("remote scrubbed of token")

    log("== 3) trigger workflow_dispatch ==")
    st, disp = api("POST", f"{API}/repos/{login}/{REPO_NAME}/actions/workflows/clean-machine-verify.yml/dispatches",
                   {"ref": BRANCH})
    log(f"dispatch: status {st} {disp if st not in (201, 204) else 'OK'}")
    if st not in (201, 204):
        sys.exit(5)

    log("== 4) poll run ==")
    for i in range(90):
        time.sleep(10)
        st, runs = api("GET", f"{API}/repos/{login}/{REPO_NAME}/actions/runs?event=workflow_dispatch&per_page=1")
        if st == 200 and runs.get("total_count", 0) > 0:
            run = runs["workflow_runs"][0]
            log(f"  [{i}] run {run['id']} status={run['status']} conclusion={run.get('conclusion')} {run['html_url']}")
            if run["status"] == "completed":
                log(f"FINAL: conclusion={run.get('conclusion')}")
                sys.exit(0 if run.get("conclusion") == "success" else 1)
    log("TIMEOUT waiting for run completion")
    sys.exit(2)

if __name__ == "__main__":
    main()
