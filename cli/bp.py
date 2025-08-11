#!/usr/bin/env python3
import os, sys, json, time, subprocess, signal, importlib.util, re, hashlib, random
from pathlib import Path
import click, requests

# ---- storage
BP_DIR        = Path.cwd() / "bp_files"
CONFIG_NAME   = BP_DIR / ".bp-config.json"
PID_NAME      = BP_DIR / ".bp-agent.pid"
HISTORY_NAME  = BP_DIR / ".bp-history.json"
LOG_NAME      = BP_DIR / ".bp-agent.log"

# ---- API autodetect (prefer backend:5000)
API_CANDIDATES = [
    os.getenv("BP_API_URL", "").strip() or "http://localhost:5000",
    "http://127.0.0.1:5000",
    "http://localhost:8080",
]

# ---- watcher knobs
DEBOUNCE_SECONDS   = int(os.getenv("BP_DEBOUNCE_SECONDS", "10"))
INPUT_DEDUPE_TTL   = int(os.getenv("BP_INPUT_DEDUPE_TTL", "180"))
FILE_COOLDOWN_SEC  = int(os.getenv("BP_FILE_COOLDOWN_SEC", "300"))

# ---- scope
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

MAX_FILES_PER_BATCH = 200
MAX_BYTES_PER_FILE  = 400_000

IGNORE_BASENAME_REGEXES = [r"^\.?#.*#$", r"^~\$.*", r".*\.swp$", r".*\.swx$", r".*\.tmp$", r".*\.temp$", r".*~$"]

def info(msg): click.echo(msg)
def err(msg): click.echo(msg, err=True)
def ensure_bp_dir(): BP_DIR.mkdir(exist_ok=True)
def _need_watchdog() -> bool: return importlib.util.find_spec("watchdog") is None

def _hash_bytes(b: bytes) -> str: return hashlib.sha256(b).hexdigest()
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
    return {"last_run": 0, "file_seen": {}, "last_notify": {}, "recent_batch_ids": []}

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
            out.append({"name": p.name, "path": str(p.relative_to(root)), "content": content})
            if len(out) >= MAX_FILES_PER_BATCH: return out
    return out

def _collect_specific(root: Path, paths: set[str], include_ext: set, include_names: set, exclude_dirs: set) -> list[dict]:
    out = []
    for rel in sorted(paths):
        p = (root / rel).resolve()
        try: p.relative_to(root.resolve())
        except Exception: continue
        if any(part in exclude_dirs for part in p.relative_to(root).parts[:-1]): continue
        if not p.exists() or not p.is_file(): continue
        if not _in_scope(p, include_ext, include_names): continue
        try:
            if p.stat().st_size > MAX_BYTES_PER_FILE: continue
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        out.append({"name": p.name, "path": str(p.relative_to(root)), "content": content})
        if len(out) >= MAX_FILES_PER_BATCH: break
    return out

def _parse_env_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"): continue
        if "=" in t:
            k = t.split("=",1)[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k): keys.add(k)
    return keys

def _scan_compose_hints(files: list[dict]) -> dict:
    dockerfile_paths = []
    compose_named_vols = set()
    compose_builds = []
    compose_env_refs = set()
    svc_block_re = re.compile(r"(?ms)^\s*services:\s*\n(?P<body>.+?)(?:^\S|\Z)")
    service_hdr_re = re.compile(r"^\s*([A-Za-z0-9._-]+):\s*\n(?:(?:\s{2,}.+\n)+)", re.M)
    build_line_re  = re.compile(r"^\s{2,}build:\s*(.+)$", re.M)
    volumes_top_re = re.compile(r"(?ms)^\s*volumes:\s*\n(?P<body>(?:\s{2,}[A-Za-z0-9._-]+\s*:.*\n?)+)")
    env_ref_re     = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for f in files:
        path = f.get("path") or f.get("name")
        low = (path or "").lower()
        if low.endswith("dockerfile") or low.endswith(".dockerfile"):
            dockerfile_paths.append(path)
        if low.endswith(("docker-compose.yml","docker-compose.yaml","compose.yml","compose.yaml")):
            text = f.get("content","")
            m = volumes_top_re.search(text)
            if m:
                for line in m.group("body").splitlines():
                    m2 = re.match(r"^\s{2,}([A-Za-z0-9._-]+)\s*:", line)
                    if m2: compose_named_vols.add(m2.group(1))
            ms = svc_block_re.search(text)
            if ms:
                body = ms.group("body")
                for blk in service_hdr_re.finditer(body):
                    block = blk.group(0); svc = blk.group(1)
                    b = build_line_re.search(block)
                    if b:
                        context = b.group(1).strip()
                        compose_builds.append({"file": path, "service": svc, "context": context})
            for var in env_ref_re.findall(text):
                compose_env_refs.add(var)
    return {
        "dockerfile_paths": sorted(set(dockerfile_paths)),
        "compose_named_volumes": sorted(compose_named_vols),
        "compose_builds": compose_builds,
        "compose_env_refs": sorted(compose_env_refs),
    }

def _collect_repo_hints(root: Path, include_ext: set, include_names: set, exclude_dirs: set) -> dict:
    files = _collect_files(root, include_ext, include_names, exclude_dirs)
    env_files, env_keys = [], set()
    for r, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fn in names:
            if fn.startswith(".env"):
                p = Path(r) / fn
                try: env_files.append(str(p.relative_to(root)))
                except Exception: env_files.append(fn)
                try: env_keys |= _parse_env_keys(p.read_text(encoding="utf-8", errors="ignore"))
                except Exception: pass
    hints = {"env_files": sorted(set(env_files))[:50], "env_keys": sorted(env_keys)}
    hints.update(_scan_compose_hints(files))
    return hints

def _batch_id(files: list[dict], reason: str) -> str:
    parts = []
    for f in sorted(files, key=lambda x: (x.get("path") or x.get("name") or "").lower()):
        path = f.get("path") or f.get("name") or ""
        digest = _hash_txt(f.get("content",""))
        parts.append(f"{path}:{digest}")
    seed = reason + "|" + "|".join(parts)
    return _hash_txt(seed)

def _post_batch(api_base: str, email: str, files: list[dict], repo_hints: dict, reason: str, created_paths: list[str], batch_id: str) -> dict:
    payload = {
        "email": email,
        "files": files,
        "repo_hints": repo_hints,
        "notify_reason": reason,
        "created_paths": created_paths,
        "batch_id": batch_id,          # <--- stable fingerprint for backend dedupe
    }
    r = requests.post(f"{api_base}/api/analyze_batch", json=payload, timeout=300)
    if not r.ok:
        raise RuntimeError(f"[analyze_batch] HTTP {r.status_code} {r.text[:200]}")
    return r.json()

@click.group()
def cli(): pass

@cli.command()
def init():
    ensure_bp_dir()
    root = Path.cwd()
    email = click.prompt("Enter your email for alerts", type=str).strip()

    base, ok = _detect_api_base()
    _write_config(root, email, base)
    info(f"📄 Wrote {CONFIG_NAME}")
    info("✅ Backend reachable." if ok else f"⚠️ Backend not reachable; saved api_base={base}. Initial scan may fail.")

    # initial one-shot scan
    try:
        root, api_base, email, include_ext, include_names, exclude_dirs = _load_cfg()
        info("🔍 Starting initial scan on existing files...")
        files = _collect_files(root, include_ext, include_names, exclude_dirs)
        hints = _collect_repo_hints(root, include_ext, include_names, exclude_dirs)
        if files:
            bid = _batch_id(files, "initial")
            data = _post_batch(api_base, email, files, hints, reason="initial", created_paths=[], batch_id=bid)
            issues = int(data.get("issues_found", 0))
            info(f"🚩 Detected {issues} issue(s). Email sent to {email}." if issues else "✅ No issues detected.")
    except Exception as e:
        err(f"Initial scan error: {e}")

    # start foreground watcher as a detached process
    if PID_NAME.exists():
        try:
            old = int(PID_NAME.read_text().strip()); os.kill(old, 0)
            err(f"⚠️ Agent already running (pid {old}). Use `bp stop` first."); return
        except Exception: PID_NAME.unlink(missing_ok=True)

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
@click.option("--skip-initial", is_flag=True)
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
        base, _ = _detect_api_base()
        _write_config(root, email, base)

    root, api_base, email, include_ext, include_names, exclude_dirs = _load_cfg()
    state = _load_state()
    file_seen   = state.get("file_seen", {})
    last_notify = state.get("last_notify", {})
    recent_batch_ids: list[tuple[float,str]] = state.get("recent_batch_ids", [])
    pending_paths: set[str] = set()
    pending_created: set[str] = set()
    last_event_ts = 0.0

    def _evict(now: float):
        while recent_batch_ids and (now - recent_batch_ids[0][0]) > INPUT_DEDUPE_TTL:
            recent_batch_ids.pop(0)
    def _already(bid: str, now: float) -> bool:
        _evict(now); return any(b == bid for _, b in recent_batch_ids)
    def _remember(bid: str, now: float):
        recent_batch_ids.append((now,bid)); _evict(now)

    def _maybe_send():
        nonlocal pending_paths, pending_created, last_event_ts, file_seen, last_notify, state
        now = time.time()
        if not pending_paths: return
        if (now - last_event_ts) < DEBOUNCE_SECONDS: return

        files_all = _collect_specific(root, pending_paths, include_ext, include_names, exclude_dirs)
        if not files_all: pending_paths.clear(); pending_created.clear(); return

        files_to_send, created_paths = [], []
        for f in files_all:
            path = f["path"]; digest = _hash_txt(f["content"])
            ln = last_notify.get(path)
            if ln and (now - ln.get("ts",0)) < FILE_COOLDOWN_SEC and ln.get("digest") == digest:
                continue
            files_to_send.append(f)
            if path in pending_created and not file_seen.get(path): created_paths.append(path)

        if not files_to_send: pending_paths.clear(); pending_created.clear(); return

        bid = _batch_id(files_to_send, "created" if created_paths else "modified")
        if _already(bid, now): pending_paths.clear(); pending_created.clear(); return

        for f in files_to_send: file_seen[f["path"]] = True
        hints = _collect_repo_hints(root, include_ext, include_names, exclude_dirs)
        reason = "created" if created_paths else "modified"
        try:
            data = _post_batch(api_base, email, files_to_send, hints, reason, created_paths, bid)
            issues = int(data.get("issues_found", 0))
            info(f"🚩 Detected {issues} issue(s). Email sent to {email}." if issues else "✅ No issues detected.")
            _remember(bid, now)
            for f in files_to_send:
                last_notify[f["path"]] = {"ts": now, "digest": _hash_txt(f["content"])}
            state["file_seen"] = file_seen; state["last_notify"] = last_notify; state["recent_batch_ids"] = recent_batch_ids
            _save_state(state)
        except Exception as e:
            err(f"[watch/analyze_batch] {e}")
        finally:
            pending_paths.clear(); pending_created.clear()

    class H(FileSystemEventHandler):
        def on_created(self, e): self._q(e, True)
        def on_modified(self, e): self._q(e, False)
        def _q(self, e, created: bool):
            nonlocal pending_paths, pending_created, last_event_ts
            if e.is_directory: return
            p = Path(e.src_path)
            if not _in_scope(p, include_ext, include_names): return
            try: rel = str(p.resolve().relative_to(root.resolve()))
            except Exception: return
            pending_paths.add(rel)
            if created: pending_created.add(rel)
            last_event_ts = time.time()

    obs = Observer(); h = H()
    obs.schedule(h, str(root), recursive=True); obs.start()

    if not skip_initial:
        info("🔍 Starting initial scan on existing files...")
        files = _collect_files(root, include_ext, include_names, exclude_dirs)
        hints = _collect_repo_hints(root, include_ext, include_names, exclude_dirs)
        if files:
            try:
                bid = _batch_id(files, "initial")
                data = _post_batch(api_base, email, files, hints, "initial", [], bid)
                issues = int(data.get("issues_found", 0))
                info(f"🚩 Detected {issues} issue(s). Email sent to {email}." if issues else "✅ No issues detected.")
            except Exception as e:
                err(f"[watch/initial] {e}")

    info(f"👀 Watching {root}. (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1); _maybe_send()
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop(); obs.join()

if __name__ == "__main__":
    cli()
