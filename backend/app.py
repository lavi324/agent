import os, re, time, hashlib, smtplib, ssl
from email.message import EmailMessage
from flask import Flask, request, jsonify
import requests
from datetime import datetime, timezone

app = Flask(__name__)

# ---- LLM provider ----
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("LLM_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

# ---- SMTP / email ----
SMTP_HOST  = os.getenv("SMTP_HOST", "")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER or "no-reply@example.com")
EMAIL_MIN_INTERVAL = int(os.getenv("EMAIL_MIN_INTERVAL", "120"))

# ---- Server-side knobs ----
BATCH_TTL = int(os.getenv("BP_BATCH_TTL", "180"))  # seconds
RECENT_BATCH: dict[str, float] = {}                # batch_fingerprint -> ts
EMAIL_STATE: dict[str, dict] = {}                  # email -> {"ts": float, "digest": str}
ALLOW_MULTI_MOD_EMAILS = os.getenv("ALLOW_MULTI_MOD_EMAILS", "0") == "1"

# ---------- helpers ----------
def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8","ignore")).hexdigest()

def _should_send(email: str, body: str) -> bool:
    now = time.time()
    dg = _digest(body)
    st = EMAIL_STATE.get(email)
    if st and st["digest"] == dg and (now - st["ts"]) < EMAIL_MIN_INTERVAL:
        print(f"[BP] suppressing duplicate email to {email}", flush=True)
        return False
    EMAIL_STATE[email] = {"ts": now, "digest": dg}
    return True

def send_email(to_addr: str, subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and EMAIL_FROM):
        print("Email not configured; skipping send.")
        return False
    try:
        msg = EmailMessage()
        msg["From"] = EMAIL_FROM
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        return True
    except Exception as e:
        print("send_email error:", e)
        return False

def llm_chat(messages: list[dict]) -> str:
    if LLM_PROVIDER == "groq":
        if not GROQ_API_KEY: raise RuntimeError("GROQ_API_KEY not set")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": GROQ_MODEL, "messages": messages, "temperature": TEMPERATURE}
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if not r.ok: raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:200]}")
        data = r.json(); return data["choices"][0]["message"]["content"].strip()
    elif LLM_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY not set")
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": OPENROUTER_MODEL, "messages": messages, "temperature": TEMPERATURE}
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if not r.ok: raise RuntimeError(f"OpenRouter HTTP {r.status_code}: {r.text[:200]}")
        data = r.json(); return data["choices"][0]["message"]["content"].strip()
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'")

def guess_domain(name: str, content: str) -> str:
    n = (name or "").lower()
    if n.endswith(".tf") or "terraform" in content.lower(): return "Terraform"
    if n in ("dockerfile",) or n.endswith(".dockerfile"): return "Docker"
    if n in ("jenkinsfile",) or "pipeline" in content.lower(): return "Jenkins"
    if n.endswith((".yml",".yaml")):
        if "argoproj.io" in content: return "ArgoCD"
        return "Kubernetes"
    return "General"

def ai_scan(domain: str, filename: str, content: str) -> list[str]:
    """
    Single-pass LLM: return crisp one-sentence issues; [] if none.
    We also strip generic preambles.
    """
    snippet = content if len(content)<=16000 else content[:16000] + "\n...[truncated]..."
    sys_msg = (
        "You are a senior DevOps engineer. Read the file and output only concrete bad practices "
        "or risky choices as short bullets (no intros, no headings). If nothing meaningful is wrong, reply exactly: No issues"
    )
    user_msg = (
        f"Domain hint: {domain}\n"
        f"File: {filename}\n"
        "Code:\n"
        f"```\n{snippet}\n```"
    )
    try:
        txt = llm_chat([
            {"role":"system","content":sys_msg},
            {"role":"user","content":user_msg},
        ])
        print(f"[BP] LLM raw for {filename}:\n{txt}\n", flush=True)
    except Exception as e:
        print(f"LLM error: {e}", flush=True)
        return []

    # Normalize/clean
    lines = []
    for raw in txt.splitlines():
        line = raw.strip()
        if not line: continue
        # Remove list symbols / indexes
        line = line.lstrip("-*•–—0123456789.) ").strip()
        # Drop generic preambles
        if re.search(r"here('?| i)s a list|potential issues|bad practices", line, re.I):
            continue
        if re.fullmatch(r"(?i)no\s+issues(\s+found)?\.?", line):
            return []
        lines.append(line)

    # dedupe, keep order
    seen=set(); out=[]
    for w in lines:
        if w and w not in seen:
            seen.add(w); out.append(w)
    return out[:50]

def _batch_fingerprint(payload: dict) -> str:
    rid = payload.get("repo_id","")
    reason = payload.get("notify_reason","")
    created = sorted(payload.get("created_paths") or [])
    files = payload.get("files") or []
    parts = [rid, reason] + created
    for f in sorted(files, key=lambda x: (x.get("path") or x.get("name") or "").lower()):
        path = f.get("path") or f.get("name") or ""
        dig  = _digest(f.get("content",""))
        parts.append(f"{path}:{dig}")
    return _digest("|".join(parts))

def _format_email(email: str, reason: str, created_paths: list[str], results: dict) -> tuple[str,str]:
    total = sum(len(v) for v in results.values())
    files_with = [k for k,v in results.items() if v]
    ts = _now_iso()

    if reason == "initial":
        subject = f"[BadPractice Agent] Repository audit — {total} issue(s) noted"
        intro   = "BadPractice Report  •  {}\n\n".format(ts)
    elif created_paths:
        fn = created_paths[0] if created_paths else list(results.keys())[0]
        subject = f"[BadPractice Agent] New file flagged — {fn} ({total} issue(s))"
        intro   = "A newly created file was flagged. Timestamp: {}\n\n".format(ts)
    else:
        subject = f"[BadPractice Agent] File update flagged — {total} issue(s)"
        intro   = "A change introduced issues. Timestamp: {}\n\n".format(ts)

    lines = [intro, f"Issues: {total}   Files affected: {len(files_with)}\n"]
    for fname, warns in results.items():
        if not warns: continue
        lines.append(f"▶ {fname}  —  {len(warns)} issue(s)")
        lines.append("-"*38)
        for i,w in enumerate(warns,1):
            lines.append(f"{i}. {w}")
        lines.append("")

    lines.append("Automated by BadPractice Agent. Reply to this email if you need help triaging.\n")
    body = "\n".join(lines)
    return subject, body

# ---------- routes ----------
@app.get("/api/ping")
def ping():
    return jsonify({"status": "ok", "message": "pong", "provider": LLM_PROVIDER})

@app.post("/api/analyze_batch")
def analyze_batch():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    files = data.get("files") or []
    reason = data.get("notify_reason") or "initial"
    created_paths = data.get("created_paths") or []
    repo_id = data.get("repo_id") or ""

    # server dedupe
    fp = _batch_fingerprint(data)
    now = time.time()
    for k in list(RECENT_BATCH.keys()):
        if now - RECENT_BATCH[k] > BATCH_TTL: del RECENT_BATCH[k]
    if fp in RECENT_BATCH:
        return jsonify({"files_analyzed": len(files), "issues_found": 0, "email_sent": False, "deduped": True})
    RECENT_BATCH[fp] = now

    # Optional guard: block multi-file "modified" mails (the 'extra email' you don't want)
    if (not ALLOW_MULTI_MOD_EMAILS) and reason == "modified" and not created_paths and len(files) > 1:
        return jsonify({
            "files_analyzed": len(files),
            "issues_found": 0,
            "email_sent": False,
            "suppressed": "multi-file-modified"
        })

    # run scan
    results = {}
    total = 0
    for f in files:
        name = f.get("path") or f.get("name") or "unknown"
        content = f.get("content","")
        domain = guess_domain(name, content)
        warns = ai_scan(domain, name, content)
        results[name] = warns
        total += len(warns)

    sent = False
    if email and total>0:
        subject, body = _format_email(email, reason, created_paths, results)
        if _should_send(email, body):
            sent = send_email(email, subject, body)

    return jsonify({
        "files_analyzed": len(files),
        "issues_found": total,
        "email_sent": bool(sent),
        "results": results,
        "dedupe_fp": fp,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
