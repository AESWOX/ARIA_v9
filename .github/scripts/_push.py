#!/usr/bin/env python
# _push.py — push with credential helper disabled, terminal prompt off.
import os
import subprocess
import sys

REPO = r"C:\Users\User\Desktop\ARIA_v9"
LOGIN = "AESWOX"
REPO_NAME = "ARIA_v9"
BRANCH = "main"

def log(msg):
    print(msg, flush=True)

def sh(cmd, timeout=600):
    log(f"  $ {' '.join(cmd[:4])} ...")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace")[-1500:]
        err = (e.stderr or b"").decode(errors="replace")[-1500:]
        log(f"TIMEOUT: {out}\n{err}")
        return None
    if r.returncode != 0:
        log(f"  rc={r.returncode}\n  stdout: {r.stdout[-1200:]}\n  stderr: {r.stderr[-1200:]}")
    else:
        log(f"  OK: {r.stdout[-400:]}")
    return r

PUSH_URL = f"https://{LOGIN}:{os.environ['GITHUB_TOKEN_2']}@github.com/{LOGIN}/{REPO_NAME}.git"

log("== push ==")
r = sh(["git", "-c", "credential.helper=", "push", PUSH_URL, f"HEAD:{BRANCH}", "--force"], timeout=600)
if r is None or r.returncode != 0:
    log("FATAL: push failed")
    sys.exit(4)
log("PUSH OK")

log("== scrub remote ==")
r = sh(["git", "remote", "set-url", "origin", f"https://github.com/{LOGIN}/{REPO_NAME}.git"], timeout=30)
if r.returncode == 0:
    log("remote scrubbed")
sys.exit(0)
