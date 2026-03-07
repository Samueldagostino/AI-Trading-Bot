# Security Audit — NQ Trading System

**Date:** 2026-02-28
**Scope:** Full repository (`Samueldagostino/AI-Trading-Bot`)
**Branch:** `main` + `claude/enable-agent-teams-MBsOm`
**Verdict:** CONDITIONAL PASS

Zero exploitable vulnerabilities in the **deployed GitHub Pages** (static HTML, no auth, no server). The Python backend (not deployed) has findings that must be fixed **before paper/live trading**: unauthenticated dashboard kill-switch, wildcard CORS, and auth response logging. No secrets in git history. No XSS. No SQL injection.

---

## Executive Summary

| Category | Result |
|----------|--------|
| Exposed Credentials | **NONE** — all secrets via `os.getenv()`, zero in git history |
| XSS / Code Injection | **NONE** — no `eval`, no `dangerouslySetInnerHTML`, safe JSON.parse |
| SQL Injection | **NONE** — asyncpg parameterized queries throughout |
| Secrets in Git History | **NONE** — only `.env.example` with placeholders |
| Dashboard Server | **HIGH** — kill-switch unauthenticated + CORS `*` (see SRV-1, SRV-2) |
| Auth Logging | **HIGH** — full API response logged on auth failure (see LOG-1) |
| CDN Supply Chain | **MEDIUM** — no SRI hashes (see CDN-1) |
| Repo Hygiene | **MEDIUM** — tracked `__pycache__`, large data blobs (see REPO-1, REPO-2) |
| Data Integrity Manifest | **LOW** — 1 hash drifted, covers 3/12 files (see DATA-1, DATA-2) |

---

## Findings

### HIGH Severity

#### SRV-1: Dashboard Kill-Switch Has No Authentication

**Location:** `nq_bot_vscode/dashboard/server.py:165-178`
**Risk:** `POST /api/kill-switch` and `POST /api/kill-switch/reset` have zero authentication. Combined with wildcard CORS (SRV-2), any website can halt or resume trading.
**Impact:** HIGH — in paper/live trading, a malicious page could toggle the kill-switch remotely.
**Fix:** Add bearer token or API key middleware before exposing state-mutating endpoints:
```python
async def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != os.getenv("DASHBOARD_API_KEY", ""):
        raise HTTPException(status_code=403)

@app.post("/api/kill-switch", dependencies=[Depends(verify_api_key)])
```
**Note:** Dashboard server is local-only (not deployed to GitHub Pages). Risk applies only when running the bot locally.

#### SRV-2: Dashboard CORS Allows All Origins

**Location:** `nq_bot_vscode/dashboard/server.py:34-39`
**Risk:** `allow_origins=["*"]` lets any origin make requests to all dashboard endpoints, including the kill-switch.
**Fix:** Restrict to localhost: `allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"]`

#### LOG-1: Sensitive Data Logged on Auth Failure

**Location:** `nq_bot_vscode/Broker/tradovate_client.py:195-196`
**Risk:** On missing access token, `logger.error(f"No access token in response: {data}")` logs the full API response, which may contain echoed credentials or account info.
**Fix:** Log only safe metadata: `logger.error(f"No access token in response. Keys: {list(data.keys())}")`
**Also:** Line 187-188 logs full response body on auth failure (`logger.error(f"Auth failed [{resp.status}]: {body}")`). Truncate: `body[:200]`.

#### ENV-1: Custom .env Parser Lacks Quoting Support

**Location:** `nq_bot_vscode/scripts/run_paper.py:48-55`
**Risk:** Hand-rolled `.env` parser doesn't handle quoted values or special characters. `TRADOVATE_PASSWORD="my p@ss"` would include literal quotes. Could cause silent auth failures with real credentials.
**Fix:** Replace with `python-dotenv`: `from dotenv import load_dotenv; load_dotenv(project_dir / ".env")`

---

### MEDIUM Severity

#### CDN-1: No Subresource Integrity (SRI) Hashes on CDN Scripts

**Location:** All HTML files in `docs/` (10 CDN scripts total)
**Risk:** CDN compromise would execute tampered scripts undetected.
**Impact:** Medium — pages are read-only dashboards with no auth, no sessions, nothing to steal. But an attacker could inject arbitrary script.
**Fix:** Add `integrity="sha384-..."` and `crossorigin="anonymous"` to all `<script>` tags.

| File | Library | Version Pinned? | SRI? |
|------|---------|----------------|------|
| `analyzer.html` | React 18 | Major only (`@18`) | No |
| `analyzer.html` | ReactDOM 18 | Major only (`@18`) | No |
| `analyzer.html` | Babel standalone | **Unpinned** | No |
| `weekly-dashboard.html` | React 18 | Major only (`@18`) | No |
| `weekly-dashboard.html` | ReactDOM 18 | Major only (`@18`) | No |
| `weekly-dashboard.html` | Babel standalone | **Unpinned** | No |
| `dashboard.html` | React 18.2.0 | Exact | No |
| `dashboard.html` | ReactDOM 18.2.0 | Exact | No |
| `dashboard.html` | Babel 7.23.9 | Exact | No |
| `report.html` | Chart.js 4.4.1 | Exact | No |

#### CDN-2: Unpinned/Partially Pinned CDN Versions

**Location:** `docs/analyzer.html:7-9`, `docs/weekly-dashboard.html:7-9`
**Risk:** `@babel/standalone` is completely unpinned. `react@18` resolves to latest 18.x minor.
**Fix:** Pin to exact versions matching `dashboard.html`: `react@18.2.0`, `react-dom@18.2.0`, `@babel/standalone@7.23.9`.

#### REPO-1: 20 Tracked `__pycache__/*.pyc` Files

**Location:** `nq_bot_vscode/**/__pycache__/`
**Risk:** Bytecode can be decompiled to recover source. Bloats repo. Files were committed before `.gitignore` rule existed.
**Fix:** `git rm -r --cached "nq_bot_vscode/**/__pycache__/"`

#### REPO-2: Large Data Files in Git History (~100MB+)

**Location:** Git object store (permanent)
**Details:**
- `September (2024) - August (2025) (12-months).txt` (20MB) — **still tracked**
- `docs/viz_data_full.json` (10.9MB) — still tracked, intentional for GitHub Pages
- Multiple deleted 10MB+ `.txt` files remain in history
- `files (1).zip` committed then deleted but in history

**Risk:** Bloats clone size. Raw trading data recoverable by anyone who clones.
**Fix:**
1. `git rm --cached "September (2024) - August (2025) (12-months).txt"`
2. Add `*.txt`, `!README.txt`, `*.zip` to `.gitignore`
3. Optional: `git filter-repo` to purge from history (requires force push)

#### DSN-1: Database DSN Contains Cleartext Password

**Location:** `nq_bot_vscode/config/settings.py:50-52`
**Risk:** Password interpolated into DSN string. If DSN is logged in a traceback or error message, password is exposed.
**Fix:** Use asyncpg keyword-based connection, or ensure DSN is never logged.

---

### LOW Severity

#### DATA-1: Validation Manifest Hash Drift

**Location:** `docs/VALIDATION_MANIFEST.sha256`
**Details:** `weekly-dashboard.html` fails SHA-256 verification — modified (nav bar added) after manifest generated.
**Fix:** Regenerate manifest after docs/ changes.

#### DATA-2: Manifest Coverage Gap

**Location:** `docs/VALIDATION_MANIFEST.sha256`
**Details:** Covers 3 files. Missing: `index.html`, `analyzer.html`, `dashboard.html`, `report.html`, `sample_viz_data.json`, `viz_data.json`, `viz_data_full.json`, `assets/style.css`.
**Fix:** Expand to cover all files in `docs/`.

#### CLIENT-1: No File Size Limit on JSON Upload

**Location:** `docs/analyzer.html` — `handleFileUpload()`
**Risk:** Self-inflicted browser DoS only (no server impact).
**Fix:** Add `if (file.size > 50 * 1024 * 1024) { setLoadError("File too large (max 50MB)"); return; }`.

#### INFRA-1: Three Different CDN Providers

**Location:** unpkg.com, cdnjs.cloudflare.com, cdn.jsdelivr.net
**Fix:** Standardize on one, or vendor scripts into `docs/assets/`.

#### SCHEMA-1: `apply_schema()` Accepts Arbitrary File Paths

**Location:** `nq_bot_vscode/database/connection.py:97-103`
**Risk:** Reads and executes SQL from any path. Currently unused but dormant risk.
**Fix:** Validate path is within project directory.

---

### CLEAN — No Issues Found

#### Credentials — CLEAN
- All Tradovate credentials: `os.getenv()` (`config/settings.py:91-98`)
- PostgreSQL password: `os.getenv("PG_PASSWORD", "")` (`config/settings.py:48`)
- Discord token: `os.getenv("DISCORD_TOKEN", "")` (`config/settings.py:58`)
- `.env.example` has placeholders only. `.env` is gitignored. No `.env` file exists.
- **Full git history scan:** zero secrets ever committed.

#### XSS / Code Injection — CLEAN
- No `eval()`, `Function()`, `setTimeout` with string args in any HTML/JS
- No `dangerouslySetInnerHTML` in any React component
- `innerHTML` in `report.html` (13 instances): template literals with self-loaded JSON data only. No user strings interpolated.
- `JSON.parse` in analyzer upload: wrapped in try/catch, errors displayed via React state
- Canvas rendering: `ctx.fillText()` with numeric values only
- React components auto-escape all rendered values

#### SQL Injection — CLEAN
- asyncpg parameterized queries (`$1, $2` placeholders) throughout
- No string concatenation in SQL
- Schema from static `.sql` file

#### Command Injection — CLEAN
- `os.system("clear")` in 3 locations — hardcoded strings, no user input
- No `subprocess` with user-controlled args
- No `pickle.loads`, no `exec()`, no `yaml.unsafe_load()`

#### GitHub Pages — CLEAN
- `.nojekyll` present. HTTPS forced. Static files only. No custom domain.

#### Broker Client — CLEAN
- HTTPS and WSS URLs throughout
- Demo environment enforced via config assertion
- Tokens in instance variables, not logged at INFO level

---

## Methodology

1. **Automated scanning:** ripgrep pattern matching for credential patterns, injection sinks, dangerous functions across all file types
2. **Manual code review:** All HTML/JS in `docs/`, all Python in `config/`, `broker/`, `database/`, `dashboard/`, `execution/`, `scripts/`
3. **Git history audit:** Full commit log, all files ever added, searched for secrets patterns
4. **Dependency audit:** Inventoried all CDN scripts, checked version pinning and SRI
5. **Data integrity:** Validated SHA-256 manifest against files on disk
6. **Gitignore review:** Verified coverage of `.env`, data files, IDE configs, build artifacts

## Scope

| Area | Files Reviewed |
|------|---------------|
| Client-side HTML/JS | `docs/index.html`, `docs/analyzer.html`, `docs/weekly-dashboard.html`, `docs/dashboard.html`, `docs/report.html` |
| Python backend | `main.py`, `config/settings.py`, `broker/tradovate_client.py`, `database/connection.py`, `dashboard/server.py`, `dashboard/data_adapter.py`, `scripts/run_paper.py`, `scripts/replay_simulator.py`, `scripts/paper_monitor.py` |
| Data/config | `.gitignore`, `.env.example`, `VALIDATION_MANIFEST.sha256`, `docs/*.json` |
| Git history | Full commit log, all files ever added |

---

## Recommended Actions (Priority Order)

### Fix Before Paper Trading
1. **SRV-1 + SRV-2:** Add auth to dashboard kill-switch, restrict CORS to localhost
2. **LOG-1:** Truncate/redact auth failure logs
3. **ENV-1:** Replace hand-rolled .env parser with `python-dotenv`

### Fix Before Live Trading
4. **DSN-1:** Ensure database DSN is never logged in tracebacks
5. **CDN-1 + CDN-2:** Pin versions and add SRI hashes to all CDN scripts

### Fix When Convenient
6. **REPO-1:** Remove tracked `__pycache__` files
7. **REPO-2:** Remove large data files from tracking, update `.gitignore`
8. **DATA-1 + DATA-2:** Regenerate and expand validation manifest
9. **CLIENT-1:** Add 50MB file size limit to analyzer upload
