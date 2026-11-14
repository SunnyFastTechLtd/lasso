# Deployment guide

Reference deployment: bare-metal Linux, nginx serving the SPA and proxying `/api`,
gunicorn managed by systemd, PostgreSQL 14+ for data. Docker is not required.

Replace every `<...>` placeholder with your own values. Nothing here is site-specific.

## 1. Prepare the host

```bash
sudo useradd --system --home /srv/lasso --shell /sbin/nologin lasso
sudo mkdir -p /srv/lasso /var/log/lasso
sudo chown -R lasso:lasso /srv/lasso /var/log/lasso
```

Create the database (PostgreSQL 14+):

```sql
CREATE ROLE lasso LOGIN PASSWORD '<strong-password>';
CREATE DATABASE lasso OWNER lasso;
```

Keep the cluster timezone consistent with the application; the UI formats timestamps in the
server timezone, so a mismatch shows up as a fixed hour offset in the audit log.

## 2. Deploy the code

```bash
sudo -u lasso git clone <your-repository-url> /srv/lasso
cd /srv/lasso
sudo -u lasso python3.11 -m venv .venv
sudo -u lasso .venv/bin/pip install -r requirements.txt
sudo -u lasso cp .env.example .env      # then edit .env
sudo -u lasso .venv/bin/python manage.py migrate
sudo -u lasso .venv/bin/python manage.py createsuperuser
```

`.env` must set at least: `DEBUG=false`, `SECRET_KEY`, `JWT_SIGNING_KEY`, `MCP_SECRET_KEY`,
`ALLOWED_HOSTS`, `DB_*`, and the `LLM_*` block (or configure the model in the admin UI later).

## 3. Build the frontend

```bash
cd /srv/lasso/frontend
npm ci
npm run build            # outputs frontend/dist
```

Build on the server if the architecture differs, otherwise build in CI and copy `dist/`.

## 4. systemd unit

```ini
[Unit]
Description=Lasso API
After=network.target postgresql.service

[Service]
Type=simple
User=lasso
WorkingDirectory=/srv/lasso
EnvironmentFile=/srv/lasso/.env
ExecStart=/srv/lasso/.venv/bin/gunicorn backend.wsgi:application \
  --bind 127.0.0.1:8000 --workers 3 --timeout 120
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lasso
sudo systemctl status lasso
```

A copy of this unit is in [`deploy/systemd/lasso.service`](../deploy/systemd/lasso.service).

Long-running ingestion (knowledge chunking and the Tiangong collection) runs in Celery. With a
worker online the create request returns immediately and the work continues in the background;
without one the tasks run inline in the web request. Install the worker unit next to the web unit:

```bash
sudo cp deploy/systemd/lasso-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasso-worker
sudo systemctl status lasso-worker
```

It requires a reachable Redis and these `.env` values; restart `lasso` afterwards because the web
process reads the eager flag at startup:

```dotenv
CELERY_TASK_ALWAYS_EAGER=False
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2
```

### Knowledge queue and OCR

Document chunking and OCR ingestion are routed to a dedicated `knowledge` queue so that
heavy OCR jobs cannot starve Tiangong collection. Run a second worker unit next to
`lasso-worker` (which keeps consuming the default queue unchanged):

```bash
sudo cp deploy/systemd/lasso-knowledge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasso-knowledge-worker
sudo systemctl status lasso-knowledge-worker
```

Concurrency is set by `Environment=KNOWLEDGE_CONCURRENCY` in the unit:

- Multimodal-model OCR (default): `2` — a page mostly waits on the model response.
- Local tesseract OCR: `1` — tesseract saturates a CPU core, parallel pages only slow each
  other down.

`acks_late` and the `soft_time_limit=900` / `time_limit=960` pair live in
`backend/apps/knowledge/tasks.py`; the unit only handles process supervision and uses
`--prefetch-multiplier=1` so a worker does not reserve several large documents at once.

Raw-text PDFs and Office files never touch tesseract. A page is rendered and OCR'd only
when its text layer is missing or shorter than `KNOWLEDGE_OCR_MIN_TEXT_CHARS` (default 16),
so a mixed PDF is handled page by page.

OCR runs a three-level fallback (`auto`): the platform's configured multimodal model
first, then the local `tesseract` binary, and finally an explicit failure recorded in
`Document.extraction_report`. Install the local fallback on the host (this is a
deployment prerequisite; the script is idempotent and does not install anything unless
`--yes` is passed):

```bash
deploy/ops/install_ocr.sh --check     # report status, no changes
sudo deploy/ops/install_ocr.sh --yes  # dnf/apt: tesseract + chi_sim（eng 由 tesseract 自带）
```

On CentOS / RHEL 8 the AppStream repository only ships `tesseract` 4.1.1, and
`tesseract-langpack-chi_sim` lives in EPEL (the official `epel-next` repo has been 404
since the EOL). The script therefore points `dnf` at an EPEL 8 mirror with a transient
`--repofrompath` (default `https://mirrors.aliyun.com/epel/8/Everything/x86_64/`,
override with `EPEL_REPO_URL`), imports the EPEL GPG key if needed, and does not write
anything under `/etc/yum.repos.d`. It installs only `tesseract-langpack-chi_sim`:
`tesseract` already ships `eng.traineddata`, and installing `tesseract-langpack-eng`
fails the transaction with a file conflict on that path. The script is idempotent; it
clears the dnf cache before installing so a previous failed transaction cannot cause a
spurious `Already downloaded` error.

Approximate size (measured on CentOS Stream 8): tesseract about 28.8 MB installed,
chi_sim about 2.5 MB, `/usr/share/tesseract/tessdata` about 25 MB, first install
downloads about 17 MB. Verification is `tesseract --list-langs` containing both
`chi_sim` and `eng`. If it is missing, OCR still works through the multimodal model;
when both are unavailable the affected pages are listed under
`extraction_report.failed_pages` with a reason, and a document whose pages all fail is
marked `failed` instead of silently completing with zero chunks.

### Tiangong L3: Bing fallback (Playwright + Chromium)

L3 is an **optional, default-off** fallback that only runs when the discovery switch and
the L3 switch are both on, L1 (MCP search) and L2 (SearXNG) produced no usable results,
and the browser health check passes. Install it on the host only if you intend to use L3:

```bash
deploy/install_playwright.sh --check          # report status, no changes
sudo deploy/install_playwright.sh --yes       # playwright + Chromium (headless)
sudo deploy/install_playwright.sh --yes --with-deps   # also install OS libs (dnf/apt)
```

The script installs [`deploy/requirements-playwright.txt`](../deploy/requirements-playwright.txt)
into the existing `uv` virtualenv (it falls back to `python -m pip` only if that env has pip,
and refuses to touch the system Python otherwise). Browsers go to
`${LASSO_APP_DIR:-/srv/lasso}/.cache/ms-playwright`, overridable with
`PLAYWRIGHT_BROWSERS_PATH`; the service process and the installer must use the same path.

Size: the wheel is about 40 MB, the Chromium download about 170 MB, and the on-disk
browser about 300–500 MB. A single headless instance typically holds 200–400 MB of memory;
L3 runs queries serially, so there is no concurrency multiplier.

Verification: the script checks that `playwright` imports inside the venv and that
`chromium.executable_path` exists. Afterwards an administrator can press **检查浏览器** on
the Tiangong settings page, which calls
`POST /api/knowledge/tiangong-search/bing/check/` and actually launches and closes a
headless Chromium.

If Playwright or Chromium is missing, the application does not crash and L3 does not run:
the layer returns `bing_unavailable` / `bing_browser_missing`, the reason is shown on the
settings page and written to the run's fallback chain, and L4 source-library crawling
still executes. Captcha or HTTP 429 responses are never bypassed; they are recorded as
`bing_captcha` / `bing_rate_limited` and open a 24-hour circuit for that layer. This
round intentionally does **not** install these packages on the server.

Python dependencies in this deployment are installed with `uv` (the virtualenv was
created by `uv` and has no `pip`). Do not run `pip install -r requirements.txt` in it:
it fails with "No module named pip". Use:

```bash
sudo -u lasso uv pip install --python .venv/bin/python -r requirements.txt
```

If you pass a mirror, add `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`
(direct PyPI times out on the intranet host).

Optional `.env` overrides:

```dotenv
KNOWLEDGE_OCR_MIN_TEXT_CHARS=16
KNOWLEDGE_OCR_MAX_PAGES=200
KNOWLEDGE_OCR_RENDER_SCALE=2.0
KNOWLEDGE_OCR_LANG=chi_sim+eng
KNOWLEDGE_OCR_TIMEOUT=60
KNOWLEDGE_OCR_LLM_TIMEOUT=60
KNOWLEDGE_OCR_LLM_MAX_TOKENS=4096
```

Conversation attachments keep using `enable_ocr=False` and never enter this path.

## 5. nginx

```nginx
server {
    listen 80;
    server_name <your-domain>;

    root /srv/lasso/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;   # SPA history fallback
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                # required for SSE streaming
        proxy_read_timeout 300s;
    }
}
```

Samples live in [`deploy/nginx/`](../deploy/nginx/). If media files are served through the app,
proxy `/media/` to the backend as well.

## 6. Backups and restore

Use `pg_dump` in custom format and `pg_restore` to restore:

```bash
# backup (reads connection settings from .env)
set -a; . /srv/lasso/.env; set +a
PGPASSWORD="$DB_PASSWORD" pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$DB_NAME" -Fc -f "/var/backups/lasso-$(date +%F-%H%M%S).dump"

# verify
pg_restore -l /var/backups/lasso-<timestamp>.dump | head

# restore (destructive: replaces the current database)
dropdb -h "$DB_HOST" -U "$DB_USER" "$DB_NAME"
createdb -h "$DB_HOST" -U "$DB_USER" -O "$DB_USER" "$DB_NAME"
PGPASSWORD="$DB_PASSWORD" pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$DB_NAME" --no-owner /var/backups/lasso-<timestamp>.dump
```

The PostgreSQL client major version must be **greater than or equal to** the server version.
`pg_dump` from an older major version refuses to dump a newer server.

Roll back a release with a database restore plus a `git checkout <previous-tag>`, then rebuild
the frontend and restart the service. Migrations are forward-only; do not hand-write reverse SQL.

## 7. Verify a deployment

```bash
systemctl is-active lasso
curl -s -o /dev/null -w '%{http_code}\n' http://<your-domain>/login          # 200
curl -s -o /dev/null -w '%{http_code}\n' http://<your-domain>/api/auth/profile/  # 401 anonymous
journalctl -u lasso --since '10 min ago' | grep -c Traceback                  # 0
```

## 8. Common problems

| Symptom | Likely cause |
|---|---|
| 502 from nginx | gunicorn not running, or bound to a different port |
| Login succeeds but every page 401s | `JWT_SIGNING_KEY` changed, or clock skew > token lifetime |
| Chat replies arrive all at once | nginx buffering not disabled for `/api/` |
| Timestamps off by hours | database cluster timezone differs from the app host |
| `pg_dump: server version ... aborting` | client major version older than the server |
| Import approval button returns 403 | the request was submitted by the same administrator |
| Tiangong L3 shows `未安装 Playwright` / `Chromium 未安装` | optional browser prerequisite not installed — run `deploy/install_playwright.sh --check` (L1/L2/L4 are unaffected) |
