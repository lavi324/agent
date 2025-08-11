import os, re, smtplib, ssl, time, hashlib, random
from email.message import EmailMessage
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ---- LLM config (deterministic by default)
LLM_PROVIDER    = os.getenv("LLM_PROVIDER", "groq").lower()
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
LLM_MODEL       = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
LLM_TOP_P       = float(os.getenv("LLM_TOP_P", "0"))
LLM_RETRY_MAX        = int(os.getenv("LLM_RETRY_MAX", "6"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.5"))

PROMPT_CHAR_BUDGET = int(os.getenv("BP_PROMPT_CHAR_BUDGET", "120000"))
PER_FILE_MAX_CHARS = int(os.getenv("BP_PER_FILE_MAX_CHARS", "16000"))

# ---- Email
SMTP_HOST  = os.getenv("SMTP_HOST", "")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER or "no-reply@example.com")
EMAIL_MIN_INTERVAL = int(os.getenv("EMAIL_MIN_INTERVAL", "120"))

# In-memory state
EMAIL_STATE: dict[str, dict] = {}            # last body digest per email
RECENT_BATCH_IDS: dict[str, list] = {}       # email -> [(ts, batch_id)]
BATCH_TTL = int(os.getenv("BP_BATCH_TTL", "180"))

SEPARATOR = "—" * 72

def _should_send_by_batch(email: str, batch_id: str) -> bool:
    now = time.time()
    arr = RECENT_BATCH_IDS.setdefault(email, [])
    # evict old
    RECENT_BATCH_IDS[email] = [(ts,b) for (ts,b) in arr if (now - ts) <= BATCH_TTL]
    if any(b == batch_id for _, b in RECENT_BATCH_IDS[email]):
        print(f"[BP] suppressing duplicate by batch_id for {email}", flush=True)
        return False
    RECENT_BATCH_IDS[email].append((now, batch_id))
    return True

def send_email(to_addr: str, subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and EMAIL_FROM):
        print("Email not configured; skipping send.", flush=True)
        return False
    try:
        msg = EmailMessage()
        msg["From"] = EMAIL_FROM; msg["To"] = to_addr; msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        return True
    except Exception as e:
        print("send_email error:", e, flush=True); return False

def _groq_chat_with_retry(messages: list[dict]) -> str:
    if LLM_PROVIDER != "groq": raise RuntimeError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'")
    if not GROQ_API_KEY:       raise RuntimeError("GROQ_API_KEY not set")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": LLM_MODEL, "messages": messages, "temperature": LLM_TEMPERATURE, "top_p": LLM_TOP_P}

    last_err = None
    for attempt in range(1, LLM_RETRY_MAX + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            if r.ok:
                return r.json()["choices"][0]["message"]["content"].strip()
            status = r.status_code; body = r.text[:200]
            if status in (429,500,502,503,504):
                delay = min(20, LLM_RETRY_BASE_DELAY * (2 ** (attempt-1)) + random.random())
                print(f"[BP] LLM retry {attempt}/{LLM_RETRY_MAX} after {delay:.1f}s (HTTP {status}): {body}", flush=True)
                time.sleep(delay); last_err = f"Groq HTTP {status}: {body}"; continue
            r.raise_for_status()
        except Exception as e:
            last_err = str(e)
            delay = min(20, LLM_RETRY_BASE_DELAY * (2 ** (attempt-1)) + random.random())
            print(f"[BP] LLM exception, retry {attempt}/{LLM_RETRY_MAX} after {delay:.1f}s: {e}", flush=True)
            time.sleep(delay)
    raise RuntimeError(last_err or "LLM request failed")

def _compose_repo_hints(hints: dict) -> str:
    parts = []
    if hints.get("env_files"): parts.append("Env files: " + ", ".join(hints["env_files"][:10]))
    if hints.get("dockerfile_paths"): parts.append("Dockerfiles: " + ", ".join(hints["dockerfile_paths"][:10]))
    if hints.get("compose_named_volumes"): parts.append("Compose volumes: " + ", ".join(hints["compose_named_volumes"]))
    if hints.get("compose_builds"):
        parts.append("Compose build contexts: " + "; ".join(f"{b['service']}=>{b['context']}" for b in hints["compose_builds"][:12]))
    if hints.get("compose_env_refs"): parts.append("Compose env refs: " + ", ".join(hints["compose_env_refs"][:12]))
    return "\n".join(parts)

def _trim_files(files: list[dict]) -> list[dict]:
    total = 0; out = []
    for f in sorted(files, key=lambda x: (x.get("path") or x.get("name") or "").lower()):
        content = f.get("content","")
        if len(content) > PER_FILE_MAX_CHARS:
            content = content[:PER_FILE_MAX_CHARS] + "\n...[truncated]..."
        if total + len(content) > PROMPT_CHAR_BUDGET: break
        g = dict(f); g["content"] = content; out.append(g); total += len(content)
    return out

def _build_prompt(files: list[dict], hints: dict, reason: str, created_paths: list[str]) -> list[dict]:
    repo_ctx = _compose_repo_hints(hints)
    intro = (
        "You are a senior DevOps reviewer. Provide a crisp email body:\n"
        "1) Executive summary. 2) Per-file sections (only files with issues) with short bullets (what/why/fix).\n"
        "Scope: Docker, Docker Compose, Kubernetes, Argo, Terraform, Jenkins, MongoDB, CI/CD.\n"
        "If everything looks good, respond briefly with 'All clear'.\n"
    )
    if reason == "created": intro += "Context: these files were just CREATED by the user.\n"
    elif reason == "modified": intro += "Context: these files were MODIFIED by the user.\n"
    else: intro += "Context: initial repository audit.\n"
    if created_paths: intro += f"New files: {', '.join(created_paths[:8])}.\n"

    blocks = []
    for f in files:
        path = f.get("path") or f.get("name") or "unknown"
        content = f.get("content","")
        blocks.append(f"### FILE: {path}\n```\n{content}\n```")
    bundle = "\n\n".join(blocks)

    return [
        {"role": "system", "content": intro},
        {"role": "user", "content": f"Repository context (stable hints):\n{repo_ctx}\n\nFiles to review:\n{bundle}"}
    ]

def _infer_count(email_body: str) -> int:
    c = 0
    for line in email_body.splitlines():
        s = line.strip()
        if not s: continue
        if re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)", s): c += 1
    return c

@app.get("/api/ping")
def ping():
    return jsonify({"status":"ok","message":"pong","provider":LLM_PROVIDER})

@app.post("/api/analyze_batch")
def analyze_batch():
    data = request.get_json(force=True)
    email   = (data.get("email") or "").strip()
    files   = data.get("files", [])
    hints   = data.get("repo_hints") or {}
    reason  = (data.get("notify_reason") or "initial").lower()
    created_paths = data.get("created_paths") or []
    batch_id = (data.get("batch_id") or "").strip()

    files = _trim_files(files)
    # server-side dedupe: one email per identical batch within TTL
    if email and batch_id and not _should_send_by_batch(email, batch_id):
        return jsonify({"files_analyzed": len(files), "issues_found": 0, "email_sent": False, "results": {}})

    try:
        messages = _build_prompt(files, hints, reason, created_paths)
        body = _groq_chat_with_retry(messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    issues = _infer_count(body)
    if reason == "created":
        subject = (f"[BadPractice Agent] New file flagged — {issues} issue(s) in "
                   f"{files[0].get('path') or files[0].get('name')}" if len(files)==1
                   else f"[BadPractice Agent] New files flagged — {issues} issue(s) across {len(files)} file(s)")
    elif reason == "modified":
        subject = f"[BadPractice Agent] Update flagged — {issues} issue(s) across {len(files)} file(s)"
    else:
        subject = f"[BadPractice Agent] Repository audit — {issues} issue(s) noted" if issues else \
                  "[BadPractice Agent] Repository audit — all clear"

    wrapped = f"{SEPARATOR}\n{body}\n{SEPARATOR}"
    sent = False
    if email:
        sent = send_email(email, subject, wrapped)

    print(f"[BP] results summary: issues_found={issues} email_sent={sent} reason={reason} files={len(files)}", flush=True)
    return jsonify({"files_analyzed": len(files), "issues_found": issues, "email_sent": bool(sent), "results": {}})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
