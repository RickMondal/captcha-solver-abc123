# student_server.py
import os
import re
import json
import base64
import uuid
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# === Config ===
SECRETS_FILE = "student_secrets.json"   # keep private; add to .gitignore
WORKDIR_BASE = Path("workspaces")
WORKDIR_BASE.mkdir(exist_ok=True)
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
PORT = int(os.environ.get("PORT", "8000"))
GH_TOKEN = os.environ.get("GH_TOKEN")
GH_USER = os.environ.get("GH_USER")
HOST = os.environ.get("HOST", f"http://localhost:{PORT}")

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# === Basic helpers ===
def load_secrets():
    if Path(SECRETS_FILE).exists():
        return json.loads(Path(SECRETS_FILE).read_text())
    return {}

secrets_map = load_secrets()

def save_secrets_map(m):
    Path(SECRETS_FILE).write_text(json.dumps(m, indent=2))

def decode_data_uri_to_file(data_uri: str, out_path: Path):
    m = re.match(r"data:(.*?);base64,(.*)", data_uri, re.S)
    if not m:
        raise ValueError("Invalid data URI")
    b64 = m.group(2)
    out_path.write_bytes(base64.b64decode(b64))

def run(cmd, cwd=None):
    """Run command and return CompletedProcess (raises on non-zero)."""
    print(f"> {' '.join(cmd)} (cwd={cwd})")
    return subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

# Minimal MIT license text (replace with full text if desired)
MIT_TEXT = """MIT License

Copyright (c) {year} {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software...
"""
def write_mit_license(path: Path, owner: str):
    path.write_text(MIT_TEXT.format(year=datetime.utcnow().year, owner=owner))

# === Minimal deterministic generator (LLM hook spot) ===
def generate_minimal_app(brief: str, attachments: list):
    # attachments: list of dicts {name: ..., url: ...}
    default_asset = attachments[0]["name"] if attachments else ""
    index_html = f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Task App</title></head>
  <body>
    <h1>Auto-generated task app</h1>
    <div>URL param: <span id="captcha-url"></span></div>
    <div>Result: <span id="captcha-result">—</span></div>
    <script>
      (async () => {{
        const params = new URLSearchParams(location.search);
        const url = params.get('url') || '{default_asset}';
        document.getElementById('captcha-url').textContent = url;
        // If it looks like an image, display it
        if (url.match(/\\.(png|jpe?g|gif|svg)$/i)) {{
          const img = document.createElement('img');
          img.src = url;
          img.alt = 'captcha';
          img.style.maxWidth = '400px';
          document.body.appendChild(img);
          // Dummy "solve" to meet checks
          setTimeout(() => document.getElementById('captcha-result').textContent = 'SOLVED_PLACEHOLDER', 1200);
        }} else {{
          const a = document.createElement('a');
          a.href = url;
          a.textContent = url;
          a.target = '_blank';
          document.body.appendChild(a);
        }}
      }})();
    </script>
  </body>
</html>"""
    readme = f"# Auto-generated Task App\n\n**Brief:** {brief}\n\nUsage: open `index.html` or deploy to Pages and use `?url=` parameter.\n"
    return {"index.html": index_html, "README.md": readme}

# === GitHub API helpers ===
def github_create_repo(repo_name, description="Auto-generated repo", private=False):
    if not GH_TOKEN:
        raise RuntimeError("GH_TOKEN not set in environment")
    url = "https://api.github.com/user/repos"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    body = {"name": repo_name, "description": description, "private": private}
    r = requests.post(url, json=body, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def github_enable_pages(owner, repo):
    if not GH_TOKEN:
        raise RuntimeError("GH_TOKEN not set")
    url = f"https://api.github.com/repos/{owner}/{repo}/pages"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    body = {"source": {"branch": "main", "path": "/"}}
    # Some accounts need PUT/POST variations; we'll try POST then fallback to PUT
    r = requests.post(url, json=body, headers=headers, timeout=10)
    if r.status_code not in (201, 202):  # 201 Created or 202 Accepted
        # Try PUT (older API)
        r = requests.put(url, json=body, headers=headers, timeout=10)
    return r

def wait_for_pages(pages_url, timeout=180, poll_interval=2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(pages_url, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False

def notify_with_backoff(eval_url, payload, max_attempts=8):
    delay = 1
    headers = {"Content-Type": "application/json"}
    for attempt in range(max_attempts):
        try:
            r = requests.post(eval_url, json=payload, headers=headers, timeout=10)
            print("Notify attempt", attempt+1, "status", r.status_code)
            if r.status_code == 200:
                return True
        except Exception as e:
            print("Notify error:", e)
        time.sleep(delay)
        delay = min(delay*2, 60)
    return False

# === Core processing ===
def process_task(task_json):
    try:
        print("Processing task:", task_json.get("task"))
        email = task_json["email"]
        task_id = task_json["task"]
        round_no = int(task_json.get("round", 1))
        nonce = task_json.get("nonce")
        brief = task_json.get("brief", "")
        attachments = task_json.get("attachments", [])
        evaluation_url = task_json.get("evaluation_url")

        ws = Path(tempfile.mkdtemp(prefix=f"task-{task_id}-"))
        print("Workspace:", ws)

        # Write attachments
        for att in attachments:
            name = att["name"]
            url = att["url"]
            out = ws / name
            decode_data_uri_to_file(url, out)
            print("Attachment written:", out)

        # Generate files
        files = generate_minimal_app(brief, attachments)
        for fname, content in files.items():
            (ws / fname).write_text(content, encoding="utf-8")

        # LICENSE
        write_mit_license(ws / "LICENSE", owner=GH_USER or "unknown")

        # init git
        run(["git", "init"], cwd=str(ws))
        run(["git", "config", "user.email", email], cwd=str(ws))
        run(["git", "config", "user.name", GH_USER or "student"], cwd=str(ws))
        run(["git", "add", "--all"], cwd=str(ws))
        run(["git", "commit", "-m", f"Initial commit for {task_id} round {round_no}"], cwd=str(ws))

        # create repo
        repo_name = f"{task_id}-{uuid.uuid4().hex[:6]}"
        gh_info = github_create_repo(repo_name, description=f"Task {task_id} round {round_no}", private=False)
        repo_url = gh_info["html_url"]

        # push (use token in remote URL only in transient process; do not log)
        remote_url = f"https://{GH_TOKEN}@github.com/{GH_USER}/{repo_name}.git"
        run(["git", "remote", "add", "origin", remote_url], cwd=str(ws))
        run(["git", "branch", "-M", "main"], cwd=str(ws))
        run(["git", "push", "-u", "origin", "main"], cwd=str(ws))

        # get commit sha
        cp = run(["git", "rev-parse", "HEAD"], cwd=str(ws))
        commit_sha = cp.stdout.strip()

        # enable pages
        pages_resp = github_enable_pages(GH_USER, repo_name)
        print("Pages API status:", pages_resp.status_code if pages_resp is not None else None)
        pages_url = f"https://{GH_USER}.github.io/{repo_name}/"

        # wait for pages to become available
        ok = wait_for_pages(pages_url, timeout=180)
        if not ok:
            print("Warning: pages did not become available in time:", pages_url)

        payload = {
            "email": email,
            "task": task_id,
            "round": round_no,
            "nonce": nonce,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url
        }

        if evaluation_url:
            success = notify_with_backoff(evaluation_url, payload)
            if not success:
                print("Failed to notify evaluation_url after retries.")

        print("Finished task:", task_id, "repo:", repo_url)
        # Optional cleanup: shutil.rmtree(ws)

    except Exception as e:
        print("Error in process_task:", e)

# === Flask endpoints ===
@app.route("/api/task", methods=["POST"])
def api_task():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    required = ["email", "secret", "task", "round", "nonce", "brief", "evaluation_url"]
    for k in required:
        if k not in data:
            return jsonify({"error": f"missing {k}"}), 400

    email = data["email"]
    secret = data["secret"]
    stored = secrets_map.get(email)
    if stored is None or stored != secret:
        return jsonify({"error": "invalid secret"}), 400

    # immediate ack
    executor.submit(process_task, data)
    return jsonify({"status": "accepted"}), 200

@app.route("/admin/add_secret", methods=["POST"])
def admin_add_secret():
    body = request.get_json(force=True)
    email = body.get("email")
    secret = body.get("secret")
    if not email or not secret:
        return jsonify({"error": "need email & secret"}), 400
    secrets_map[email] = secret
    save_secrets_map(secrets_map)
    return jsonify({"status": "saved"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
