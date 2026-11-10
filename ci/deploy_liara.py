#!/usr/bin/env python3
"""Deploy /home/z/my-project/nobatyar/backend archive to Liara app 'nobatyar'.

The Liara API token validates only in intermittent windows, so the script:
  1. Acquires an exclusive flock (F2: no concurrent deploys).
  2. Polls GET /v1/me until it returns 200 (auth window open).
  3. In the window: fetches last release info — if the last release already
     carries this exact commit and is DEPLOYED/PUSHED (or still building),
     aborts (F2: no duplicate release). Otherwise mirrors its config,
     uploads the source archive, creates the release.
  4. Polls release state with 401-tolerant retries until terminal state.
Never prints the token or any Authorization header.

Optional env overrides:
  DEPLOY_DEADLINE_MIN  auth-window deadline in minutes (default 45)
  DEPLOY_REPO          repo used for git_info (default /home/z/my-project/nobatyar;
                       rollback uses a detached worktree path instead)
  DEPLOY_LOCK_FILE     lock file path (default /tmp/nobatyar_deploy.lock)
"""
import fcntl, json, os, subprocess, sys, time, datetime

TOKEN = os.environ["LIARA_TOKEN"]
APP = "nobatyar"
ARCHIVE = "/tmp/liara_source.tar.gz"
API = "https://api.liara.ir"
LOG = "/tmp/liara_deploy.log"

TERMINAL_OK = {"DEPLOYED", "PUSHED"}
TERMINAL_BAD = {"FAILED", "ERROR", "CANCELED", "DEPLOY_FAILED", "BUILD_FAILED"}

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")

def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def curl(method, path, **kw):
    # -w appends the real HTTP status code on a separate line; we return that
    # code (NOT the curl process exit code — the earlier bug that made a
    # successful upload look like a failure and retried forever).
    cmd = ["curl", "-sS", "-m", "120", "-w", "\n%{http_code}", "-X", method, "-H", f"Authorization: Bearer {TOKEN}"]
    data = kw.get("data")
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    if kw.get("upload"):
        cmd += ["-H", "Expect:", "-F", f"file=@{ARCHIVE};type=application/gzip;filename=source.tar.gz"]
    cmd.append(API + path)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    body = r.stdout.strip()
    http_code = 0
    if body.isdigit():
        http_code, body = int(body), ""
    elif "\n" in body:
        body, _, tail = body.rpartition("\n")
        if tail.strip().isdigit():
            http_code = int(tail.strip())
    try:
        return http_code, json.loads(body) if body else {}, body
    except json.JSONDecodeError:
        return http_code, {}, body

def window_open():
    _, d, _ = curl("GET", "/v1/me")
    return isinstance(d, dict) and "user" in d

def duplicate_guard(last_release, current_commit):
    """F2: جلوگیری از انتشار تکراری/موازی.

    Returns a blocking reason string when a new release must NOT be created:
      - last release is the SAME commit and already DEPLOYED/PUSHED → duplicate
      - last release is the SAME commit and still non-terminal → parallel deploy
    Returns None when deploying is allowed (or info is insufficient to judge).
    """
    if not isinstance(last_release, dict) or not last_release:
        return None
    gi = last_release.get("gitInfo") or {}
    last_commit = gi.get("commit") if isinstance(gi, dict) else None
    state = str(last_release.get("state") or last_release.get("status") or "").upper()
    rid = last_release.get("_id", "?")
    if last_commit and current_commit and last_commit == current_commit:
        if state in TERMINAL_OK:
            return f"آخرین release همین commit را منتشر کرده است (id={rid}, state={state}) — انتشار تکراری ممنوع"
        if state and state not in TERMINAL_BAD:
            return f"release همین commit هنوز در وضعیت {state} در جریان است — انتشار موازی ممنوع"
    return None

def git_info(repo):
    def git(cmd):
        r = subprocess.run(["git", "-C", repo] + cmd, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    remote = git(["ls-remote", "--get-url"]) or ""
    if "@" in remote and "://" in remote:
        remote = "https://" + remote.split("@", 1)[1]
    return {
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "message": git(["log", "--format=%B", "-n", "1", "HEAD"]),
        "commit": git(["rev-parse", "HEAD"]),
        "committedAt": git(["log", "--format=%ct", "-n", "1", "HEAD"]),
        "tags": [],
        "remote": remote,
        "author": {"email": git(["log", "--format=%aE", "-n", "1", "HEAD"]),
                    "name": git(["log", "--format=%aN", "-n", "1", "HEAD"])},
    }

def main():
    log("=== deploy script started ===")

    # ---- F2 گارد هم‌زمانی: قفل انحصاری ----
    lock_path = os.environ.get("DEPLOY_LOCK_FILE", "/tmp/nobatyar_deploy.lock")
    lock_f = open(lock_path, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log(f"deploy lock acquired: {lock_path}")
    except BlockingIOError:
        log("F2 guard: یک انتشار دیگر در جریان است (قفل گرفته شده) — خروج")
        return 4

    deadline_min = float(os.environ.get("DEPLOY_DEADLINE_MIN", "45"))
    total_deadline = time.time() + deadline_min * 60
    repo = os.environ.get("DEPLOY_REPO", "/home/z/my-project/nobatyar")
    info = git_info(repo)
    log(f"git_info.commit={info.get('commit')} branch={info.get('branch')}")
    source_id = None
    release_id = None

    # --- Phase 1: wait for auth window, then upload + create release ---
    while time.time() < total_deadline and release_id is None:
        if not window_open():
            time.sleep(15)
            continue
        log("AUTH WINDOW OPEN -> acting fast")

        # mirror last release config
        _, d, raw = curl("GET", f"/v1/projects/{APP}/releases?page=1&count=1")
        last = {}
        try:
            rels = d.get("releases") or []
            if rels:
                last = rels[0]
                log("last release fields: " + json.dumps(
                    {k: last.get(k) for k in ("_id", "port", "type", "args", "platformConfig", "state", "status")},
                    ensure_ascii=False))
        except Exception as e:
            log(f"could not parse last release: {e}; raw={raw[:200]}")

        # ---- F2 گارد انتشار تکراری ----
        block = duplicate_guard(last, info.get("commit"))
        if block:
            log("F2 guard: " + block)
            return 5

        # upload source
        log("uploading source archive (1.7MB)...")
        rc, d, raw = curl("POST", f"/v2/projects/{APP}/sources", upload=True)
        log(f"upload http_rc={rc} body={raw[:200]}")
        if rc != 200 or not (isinstance(d, dict) and d.get("sourceID")):
            log("upload failed in this window; will retry window")
            time.sleep(10)
            continue
        source_id = d["sourceID"]
        log(f"sourceID received: {source_id}")

        # git info moved to main() start (needed by F2 duplicate guard before upload)

        # Liara create-release API rejects extra platformConfig keys
        # ("platformConfig.mirrorURL" is not allowed) even though GET returns
        # them — historically validated fix: send only {"mirror","version"}.
        pc = last.get("platformConfig") if isinstance(last.get("platformConfig"), dict) else {}

        body = {
            "build": {"cache": True},
            "args": ["uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"],
            "port": 8000,
            "type": "python",
            "message": os.environ.get("RELEASE_MESSAGE") or "انتشار نوبتیار",
            "gitInfo": info,
            "platformConfig": {k: pc[k] for k in ("mirror", "version") if k in pc},
            "sourceID": source_id,
        }
        log("release body keys: " + json.dumps({k: (v if k not in ("gitInfo",) else "...") for k, v in body.items()}, ensure_ascii=False)[:300])

        rc, d, raw = curl("POST", f"/v2/projects/{APP}/releases", data=body)
        log(f"create release http_rc={rc} body={raw[:300]}")
        rid = d.get("release", {}).get("_id") if isinstance(d, dict) and isinstance(d.get("release"), dict) else None
        if rc in (200, 201) and rid:
            release_id = rid
            log(f"RELEASE CREATED: {release_id}")
        else:
            log("create release failed in this window; retrying window")
            time.sleep(10)

    if release_id is None:
        log("FAILED: no auth window opened within deadline; release not created")
        return 1
    # نگه‌داشتن قفل تا پایان فرآیند (عمداً بسته نمی‌شود؛ با خروج فرآیند آزاد می‌شود)

    # --- Phase 2: poll release state (401-tolerant) ---
    terminal = {"DEPLOYED", "PUSHED", "FAILED", "ERROR", "CANCELED", "DEPLOY_FAILED", "BUILD_FAILED"}
    last_state = None
    deadline = time.time() + 25 * 60
    while time.time() < deadline:
        rc, d, raw = curl("GET", f"/v1/releases/{release_id}")
        if rc == 200 and isinstance(d, dict) and isinstance(d.get("release"), dict):
            rel = d["release"]
            state = rel.get("state") or rel.get("status") or "?"
            if state != last_state:
                log(f"release state: {state} | queue={rel.get('queue')} | isDeployed={rel.get('isDeployed')}")
                last_state = state
            if str(state).upper() in terminal:
                log(f"TERMINAL STATE: {state}")
                return 0 if str(state).upper() in {"DEPLOYED", "PUSHED"} else 2
        else:
            if rc != 401:
                log(f"poll rc={rc} raw={raw[:150]}")
        time.sleep(15)
    log("polling deadline reached; last state known: " + str(last_state))
    return 3

if __name__ == "__main__":
    sys.exit(main())
