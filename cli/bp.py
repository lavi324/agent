#!/usr/bin/env python3
import os, sys, json, time, subprocess, signal, importlib.util, re, hashlib, threading
from pathlib import Path
import click, requests

# ---------- Paths (under ./bp_files) ----------
BP_DIR        = Path.cwd() / "bp_files"
CONFIG_NAME   = BP_DIR / ".bp-config.json"
PID_NAME      = BP_DIR / ".bp-agent.pid"
HISTORY_NAME  = BP_DIR / ".bp-history.json"
LOG_NAME      = BP_DIR / ".bp-agent.log"

# ---------- API autodetect (prefer backend:5000) ----------
API_CANDIDATES = [
    os.getenv("BP_API_URL", "").strip() or "http://localhost:5000",
    "http://127.0.0.1:5000",
    "http://localhost",
]

# ---------- Watcher knobs ----------
DEBOUNCE_SECONDS     = int(os.getenv("BP_DEBOUNCE_SECONDS", "10"))
INPUT_DEDUPE_TTL     = int(os.getenv("BP_INPUT_DEDUPE_TTL", "180"))   # identical batch TTL
FILE_COOLDOWN_SEC    = int(os.getenv("BP_FILE_COOLDOWN_SEC", "300"))  # per-file cooldown
MAX_FILES_PER_BATCH  = 200
MAX_BYTES_PER_FILE   = 400_000
SUPPRESS_MOD_AFTER_CREATE_SEC = float(os.getenv("BP_SUPPRESS_MOD_AFTER_CREATE_SEC", "3.0"))

# ---------- Scan scope ----------
EXCLUDE_DIRS_DEFAULT = {
    ".git","node_modules","build","dist",".next",".cache",
    ".venv","venv","__pycache__", ".idea",".vscode",
    "coverage",".pytest_cache","frontend/build","bp_files",
}
INCLUDE_EXT_DEFAULT   = [".tf",".tfvars",".hcl",".yaml",".yml",".json",".dockerfile"]
INCLUDE_NAMES_DEFAULT = [
    "dockerfile","jenkinsfile",
    "docker-compose.yml","docker-compose.yaml",
    "compose.yml","compose.yaml",
    "chart.yaml","kustomization.yaml","values.yaml",
]

IGNORE_BASENAME_REGEXES = [
    r"^\.?#.*#$", r"^~\$.*", r".*\.swp$", r".*\.swx$", r".*\.tmp$", r".*\.temp$", r".*~$"
]

def info(msg): click.echo(msg)
def err(msg): click.echo(msg, err=True)
def ensure_bp_dir(): BP_DIR.mkdir(exist_ok=True)
def _need_watchdog() -> bool: return importlib.util.find_spec("watchdog") is None

def _hash_txt(s: str) -> str: return hashlib.sha256(s.encode("utf-8","ignore")).hexdigest()

def _ignore_name(name: str) -> bool:
    return any(re.match(p, name) for p in IGNORE_BASENAME_REGEXES)

def _in_scope(p: Path, include_ext: set, include_names: set) -> bool:
    n = p.name.lower()
    return (not _ignore_name(n)) and (n in include_names or p.suffix.lower() in include_ext)

def _write_config(root: Path, email: str, api_base: str):
    cfg = {
        "root": str(root),
        "api_base": api_base,
        "email": email,
        "include_ext": INCLUDE_EXT_DEFAULT,
        "include_names": INCLUDE_NAMES_DEFAULT,
        "exclude_dirs": sorted(EXCLUDE_DIRS_DEFAULT),
    }
    CONFIG_NAME.write_text(json.dumps(cfg, indent=2))

def _load_cfg():
    cfg = json.loads(CONFIG_NAME.read_text())
    return (
        Path(cfg.get("root") or Path.cwd()),
        cfg.get("api_base"),
        (cfg.get("email") or "").strip(),
        set(cfg.get("include_ext", INCLUDE_EXT_DEFAULT)),
        set(n.lower() for n in cfg.get("include_names", INCLUDE_NAMES_DEFAULT)),
        set(cfg.get("exclude_dirs", EXCLUDE_DIRS_DEFAULT)) or set(EXCLUDE_DIRS_DEFAULT),
    )

def _load_state():
    if HISTORY_NAME.exists():
        try: return json.loads(HISTORY_NAME.read_text())
        except Exception: pass
    return {"last_run": 0, "file_seen": {}, "recent_hashes": []}

def _save_state(st: dict):
    try: HISTORY_NAME.write_text(json.dumps(st, indent=2))
    except Exception: pass

def _detect_api_base() -> tuple[str,bool]:
    last = None
    for base in API_CANDIDATES:
        if not base: continue
        try:
            r = requests.get(f"{base}/api/ping", timeout=3)
            r.raise_for_status(); return base, True
        except Exception as e:
            last = e
    return (next((b for b in API_CANDIDATES if b), "http://localhost:5000"), False if last else True)

def _collect_files(root: Path, include_ext: set, include_names: set, exclude_dirs: set) -> list[dict]:
    out = []
    for r, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fn in names:
            p = Path(r) / fn
            if not _in_scope(p, include_ext, include_names): continue
            try:
                if p.stat().st_size > MAX_BYTES_PER_FILE: continue
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = str(p.relative_to(root))
            out.append({"name": p.name, "path": rel, "content": content})
            if len(out) >= MAX_FILES_PER_BATCH: return out
    return out

def _collect_specific(root: Path, rel_paths: list[str], include_ext: set, include_names: set, exclude_dirs: set) -> list[dict]:
    out = []
    for rel in rel_paths:
        p = (root / rel)
        if not p.exists() or not p.is_file(): continue
        if any(part in exclude_dirs for part in p.relative_to(root).parts[:-1]): continue
        if not _in_scope(p, include_ext, include_names): continue
        try:
            if p.stat().st_size > MAX_BYTES_PER_FILE: continue
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        out.append({"name": p.name, "path": rel, "content": content})
        if len(out) >= MAX_FILES_PER_BATCH: break
    return out

def _repo_id(root: Path) -> str:
    return _hash_txt(str(root.resolve()))[:16]

def _batch_id(files: list[dict], reason: str, repo_id: str, created_paths: list[str]) -> str:
    parts = [reason, repo_id] + sorted(created_paths)
    for f in sorted(files, key=lambda x: (x.get("path") or x.get("name") or "").lower()):
        parts.append(f"{f.get('path') or f.get('name')}:{_hash_txt(f.get('content',''))}")
    return _hash_txt("|".join(parts))

def _post_batch(api_base: str, payload: dict) -> dict:
    r = requests.post(f"{api_base}/api/analyze_batch", json=payload, timeout=300)
    if not r.ok:
        raise RuntimeError(f"[analyze_batch] HTTP {r.status_code} {r.text[:400]}")
    return r.json()

@click.group()
def cli():
    """Bad Practice Agent CLI."""
    pass

@cli.command()
def init():
    ensure_bp_dir()
    root = Path.cwd()
    email = click.prompt("Enter your email for alerts", type=str).strip()
    api_base, ok = _detect_api_base()

    _write_config(root, email, api_base)
    info(f"📄 Wrote {CONFIG_NAME}")
    info("✅ Backend reachable." if ok else f"⚠️ Backend not reachable: saved api_base={api_base}. Initial scan may fail.")

    # One-time initial full repo scan (ONE email)
    try:
        root, api_base, email, include_ext, include_names, exclude_dirs = _load_cfg()
        rid = _repo_id(root)
        info("🔍 Starting initial scan on existing files...")
        files = _collect_files(root, include_ext, include_names, exclude_dirs)
        if files:
            bid = _batch_id(files, "initial", rid, [])
            payload = {
                "email": email,
                "files": files,
                "notify_reason": "initial",
                "created_paths": [],
                "batch_id": bid,
                "repo_id": rid,
            }
            data = _post_batch(api_base, payload)
            issues = int(data.get("issues_found", 0))
            info("🚩 Detected {} issue(s). Email sent to {}.".format(issues, email) if issues else "✅ No issues detected.")
    except Exception as e:
        err(f"Initial scan error: {e}")

    # Start watcher (skip any extra initial)
    if PID_NAME.exists():
        try:
            old = int(PID_NAME.read_text().strip()); os.kill(old, 0)
            err(f"⚠️ Agent already running (pid {old}). Use `bp stop` first."); return
        except Exception:
            PID_NAME.unlink(missing_ok=True)

    if _need_watchdog():
        err("⚠️ Missing 'watchdog'. Install: pip install watchdog requests click")

    python = sys.executable
    cmd = [python, __file__, "watch", "--skip-initial", str(root)]
    logf = open(LOG_NAME, "a", buffering=1)
    if sys.platform.startswith(("linux","darwin")):
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, start_new_session=True, cwd=str(root))
    else:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, cwd=str(root))
    PID_NAME.write_text(str(proc.pid))
    info(f"🟢 Agent started (pid {proc.pid}). Monitoring {root}")
    info(f"📝 Logs: {LOG_NAME}")

@cli.command()
def stop():
    ensure_bp_dir()
    if not PID_NAME.exists():
        err("No PID file found. Is the agent running here?"); sys.exit(1)
    try: pid = int(PID_NAME.read_text().strip())
    except Exception: pid = None
    try:
        if pid: os.kill(pid, signal.SIGTERM); time.sleep(0.4); info(f"🛑 Stopped agent (pid {pid}).")
    except ProcessLookupError:
        err("Process not found; removing stale PID file.")
    finally:
        PID_NAME.unlink(missing_ok=True)

@cli.command()
def status():
    ensure_bp_dir()
    if not PID_NAME.exists():
        click.echo("Agent: not running (no PID file).")
    else:
        pid = PID_NAME.read_text().strip()
        out = subprocess.run(["ps","-p",pid,"-o","pid,cmd="], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            click.echo(f"Agent: running (pid {pid})"); click.echo(out.stdout.strip())
        else:
            click.echo("Agent: not running (stale PID). Run `bp init` to start.")
    if LOG_NAME.exists():
        click.echo("\n--- tail bp_files/.bp-agent.log ---")
        try:
            for line in LOG_NAME.read_text().splitlines()[-20:]: click.echo(line)
        except Exception: pass

@cli.command()
@click.argument("root_dir", type=click.Path(exists=True), required=False)
@click.option("--skip-initial", is_flag=True, help="Skip any initial send from the watcher.")
def watch(root_dir=None, skip_initial=False):
    ensure_bp_dir()
    if _need_watchdog():
        err("Missing 'watchdog'. Install: pip install watchdog requests click")
        sys.exit(1)

    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    if not CONFIG_NAME.exists():
        root = Path(root_dir) if root_dir else Path.cwd()
        email = click.prompt("Enter your email for alerts", type=str).strip()
        api_base, _ = _detect_api_base()
        _write_config(root, email, api_base)

    root, api_base, email, include_ext, include_names, exclude_dirs = _load_cfg()
    rid = _repo_id(root)
    state = _load_state()
    file_seen = state.get("file_seen", {})  # path -> {"digest": str, "ts": float}
    recent_hashes: list[tuple[float,str]] = state.get("recent_hashes", [])
    pending_paths: set[str] = set()
    created_paths: set[str] = set()
    last_event_at = 0.0
    last_created_sent_at = 0.0
    lock = threading.Lock()

    def _purge_recent():
        cutoff = time.time() - INPUT_DEDUPE_TTL
        recent_hashes[:] = [(ts,h) for ts,h in recent_hashes if ts >= cutoff]

    def _changed_files(paths: set[str]) -> list[dict]:
        files = _collect_specific(root, sorted(paths), include_ext, include_names, exclude_dirs)
        filtered = []
        now = time.time()
        for f in files:
            path = f.get("path") or f.get("name")
            dig = _hash_txt(f.get("content",""))
            ent = file_seen.get(path)
            if ent and ent.get("digest")==dig and (now - ent.get("ts",0)) < FILE_COOLDOWN_SEC:
                continue  # unchanged within cooldown
            filtered.append(f)
        return filtered

    def _send(reason: str, files: list[dict], created: list[str]):
        if not files: return
        _purge_recent()
        bid_src = [f"{(f.get('path') or f.get('name'))}:{_hash_txt(f.get('content',''))}" for f in files]
        batch_hash = _hash_txt("|".join([reason,rid] + sorted(created) + sorted(bid_src)))
        if any(h==batch_hash for _,h in recent_hashes):
            return  # client-side duplicate suppression
        payload = {
            "email": email,
            "files": files,
            "notify_reason": reason,
            "created_paths": created,
            "repo_id": rid,
        }
        try:
            _ = _post_batch(api_base, payload)
        except Exception as e:
            err(f"[watch/analyze_batch] {e}")
            return
        now = time.time()
        for f in files:
            path = f.get("path") or f.get("name")
            file_seen[path] = {"digest": _hash_txt(f.get("content","")), "ts": now}
        recent_hashes.append((now, batch_hash))
        state["file_seen"] = file_seen
        state["recent_hashes"] = recent_hashes
        state["last_run"] = now
        _save_state(state)

    class Handler(FileSystemEventHandler):
        def _queue(self, rel):
            if any(part in exclude_dirs for part in Path(rel).parts[:-1]): return
            with lock:
                pending_paths.add(rel)
                nonlocal last_event_at
                last_event_at = time.time()

        def on_created(self, event):
            if event.is_directory: return
            p = Path(event.src_path)
            if _in_scope(p, include_ext, include_names):
                try: rel = str(p.resolve().relative_to(root.resolve()))
                except Exception: rel = p.name
                created_paths.add(rel)
                self._queue(rel)

        def on_modified(self, event):
            if event.is_directory: return
            p = Path(event.src_path)
            if _in_scope(p, include_ext, include_names):
                try: rel = str(p.resolve().relative_to(root.resolve()))
                except Exception: rel = p.name
                self._queue(rel)

    observer = Observer()
    handler = Handler()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    info(f"👀 Watching {root}. (Ctrl+C to quit)")

    # Optional: skip any immediate send when watcher starts
    start_skip_until = time.time() + (DEBOUNCE_SECONDS if skip_initial else 0)

    try:
        while True:
            time.sleep(0.3)
            with lock:
                due = (pending_paths and (time.time()-last_event_at)>=DEBOUNCE_SECONDS and time.time()>=start_skip_until)
                if not due: continue
                paths = list(pending_paths); pending_paths.clear()
                created_now = [p for p in paths if p in created_paths]
                modified_now = [p for p in paths if p not in created_paths]

                # If newly created files exist, send ONLY those now (one email per file),
                # and defer the modified ones to the next cycle to avoid the extra combined email.
                if created_now:
                    created_files = _changed_files(set(created_now))
                    for f in created_files:
                        path = f.get("path") or f.get("name")
                        _send("created", [f], [path])
                        try: created_paths.remove(path)
                        except KeyError: pass
                    # Defer modified for a bit
                    for m in modified_now:
                        pending_paths.add(m)
                    last_event_at = time.time() + SUPPRESS_MOD_AFTER_CREATE_SEC
                    continue  # next loop

            # No created files in this dispatch; send modified paths ONE BY ONE
            files = _changed_files(set(modified_now))
            for f in files:
                path = f.get("path") or f.get("name")
                _send("modified", [f], [])
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()

if __name__ == "__main__":
    cli()
