# LLM Code Deployment — Student Server

This repository contains a student-side server for the **LLM Code Deployment** assignment.
The server accepts JSON task requests, generates a minimal static app from attachments,
creates a public GitHub repository with MIT license, enables GitHub Pages, and notifies
the instructor evaluation endpoint with `{repo_url, commit_sha, pages_url}`.

> **Important:** `student_secrets.json` contains secrets used to validate incoming requests.
> NEVER commit it to a public repo. This repository's `.gitignore` omits that file.

---

## Quick start (local)

1. Clone repo and create virtualenv:
```bash
git clone https://github.com/<your-username>/<repo>.git
cd <repo>
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
