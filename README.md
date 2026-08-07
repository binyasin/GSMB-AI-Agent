# GSM Brothers — AI Recovery Calling Agent

An AI-assisted outbound calling system that contacts K-Electric consumers
about outstanding dues on GSM Brothers' behalf: reads an authorized Google
Sheet, calls eligible consumers during fixed daily windows, verifies
identity, states only sheet-sourced dues/scheme information, records the
outcome, writes it back to the Sheet and a local database, and produces a
daily report — automatically, on a schedule, with restart-safe duplicate
protection.

**Read this before deploying anything:** this is an AI-assisted recovery
*calling* system, not a deceptive or threatening one. It never invents
amounts, dates, scheme details, or K-Electric policy — see
["Compliance considerations"](#compliance-considerations).

---

## Status: what's actually verified vs. what's implemented-but-untested

Per the build brief's explicit instruction, nothing here is claimed
"production ready" until it's actually been exercised. Here's the honest
state of this build, run in an environment with **no live Twilio, Google
Cloud, or Anthropic account**:

| Component | Status |
|---|---|
| Database layer (SQLite, all 8 tables) | ✅ Verified — 126 automated tests pass |
| Google Sheets read/write logic (header-based, validation, retry) | ✅ Verified against a fixture worksheet. Real `gspread` auth code path is written and correct against the documented API, but **not exercised against a real spreadsheet** — needs `GOOGLE_SERVICE_ACCOUNT_JSON`/`FILE` + `GOOGLE_SPREADSHEET_ID` |
| Queue eligibility filtering | ✅ Verified |
| Daily scheduler state machine (all clock points in the spec, restart recovery) | ✅ Verified |
| Duplicate-call protection (atomic lock, stale-lock recovery) | ✅ Verified |
| Conversation engine (deterministic templates, LLM-classification contract, fabrication guards) | ✅ Verified with a mocked LLM. Real Anthropic call path is correct against the documented tool-use API but **not exercised live** — needs `AI_API_KEY` |
| Twilio provider (call creation, hangup, transfer, webhook signature verification) | ✅ TwiML generation and signature verification verified offline (real HMAC check, no network needed). **No real call has been placed** — needs `TWILIO_ACCOUNT_SID`/`AUTH_TOKEN`/`PHONE_NUMBER` |
| Voice webhook endpoints (`/webhooks/voice/*`) | ✅ Verified end-to-end via FastAPI TestClient with real Twilio signature computation |
| Media Streams <-> Google Speech realtime bridge | ⚠️ Implemented against the documented Twilio Media Streams + Google Cloud Speech streaming protocols. Message parsing/turn bookkeeping is unit-tested; **the actual realtime audio round-trip has not been exercised** — there is no way to do so without a live Twilio call and a Google Cloud Speech credential. See `app/webhooks/media_stream.py` docstring. |
| Campaign control API (start/pause/resume/stop/status/report/retry/sync/test-call) | ✅ Verified |
| Dashboard (Streamlit) | ✅ Verified — actually launched and confirmed serving HTTP 200 |
| Daily report generation (xlsx/csv) | ✅ Verified |
| OpenClaw skill (`openclaw-skill/`) | ✅ CLI verified to handle unreachable-server and bad-command cases correctly. **Not installed into a live OpenClaw instance** — that's a deliberate choice so your personal OpenClaw config isn't touched without you asking; see [OpenClaw setup](#openclaw-setup) |
| Full dry-run pipeline (sheet → queue → simulated call → DB → simulated sheet write → report) | ✅ Verified end-to-end — `scripts/dry_run_demo.py` |
| Docker build | Written; not built/run in this environment (no Docker daemon available here) — build it yourself and verify with `docker build .` before relying on it |

**In short:** every module is real code against the real SDKs, not a stub.
Everything that can be tested without a live external account has been
tested (126 automated tests). Nothing that requires Twilio, Google Cloud,
or Anthropic credentials has been — because none exist in this environment.
Once you add real credentials, re-run the verification steps in
["Test mode"](#test-mode) and ["Making a test call"](#making-a-test-call)
before trusting it with real consumers.

---

## Table of contents

1. [What this project does](#what-this-project-does)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Python installation](#python-installation)
5. [OpenClaw setup](#openclaw-setup)
6. [Google Cloud project](#google-cloud-project)
7. [Google Sheets API](#google-sheets-api)
8. [Service account](#service-account)
9. [Sharing the Google Sheet](#sharing-the-google-sheet)
10. [Telephony provider setup](#telephony-provider-setup)
11. [AI provider setup](#ai-provider-setup)
12. [Environment variables](#environment-variables)
13. [Local installation](#local-installation)
14. [Test mode](#test-mode)
15. [Dry-run mode](#dry-run-mode)
16. [Making a test call](#making-a-test-call)
17. [Webhook configuration](#webhook-configuration)
18. [Production deployment](#production-deployment)
19. [VPS deployment](#vps-deployment)
20. [Docker deployment](#docker-deployment)
21. [Dashboard access](#dashboard-access)
22. [Troubleshooting](#troubleshooting)
23. [Security](#security)
24. [Compliance considerations](#compliance-considerations)
25. [Start commands (quick reference)](#start-commands-quick-reference)

---

## What this project does

1. Reads consumer records from an authorized Google Sheet.
2. Filters to eligible consumers (skips Already Paid, Do Not Call,
   Completed, invalid numbers, retry-exhausted).
3. During three fixed daily windows (Asia/Karachi time), calls consumers
   one at a time (`MAX_CONCURRENT_CALLS=1` by default).
4. Verifies the caller is speaking to the actual consumer before revealing
   any account details.
5. States the outstanding amount / due date / current bill / arrears —
   **only fields present in the sheet, never invented**.
6. Explains installment/scheme information — **only if the sheet says the
   consumer is eligible**, using the sheet's own scheme text.
7. Classifies the customer's response into a fixed outcome enum (already
   paid, promise to pay, dispute, do-not-call, wrong number, etc.).
8. Writes the result to the local database first, then to the Google
   Sheet (retried later if the Sheet is temporarily unavailable — the
   database record is never lost).
9. Moves to the next eligible consumer.
10. Automatically stops at the end of each session/day and resumes at the
    next scheduled window — including correctly after a server restart.
11. Generates a daily report (`.xlsx` + `.csv`) at day's end.

## Architecture

```
Google Sheet (source of truth for consumer data)
      |
      v
Consumer Queue Manager (app/queue_manager.py)
  - eligibility filtering, phone validation
      |
      v
Scheduler (app/scheduler.py)
  - Asia/Karachi session/break windows, restart-safe
      |
      v
Calling Agent (app/calling_agent.py)
  - atomic duplicate-call lock, dial, persist
      |
      v
Telephony Provider (app/telephony/twilio_provider.py)
      |
      v
Consumer (phone call)
      |
      v
Media Streams bridge (app/webhooks/media_stream.py)
  - Google Cloud Speech STT/TTS (Urdu + English)
      |
      v
Conversation Engine (app/conversation_engine.py)
  - deterministic templates (amounts/dates/scheme) + LLM NLU (intent only)
      |
      +----> Google Sheet (write-back)
      |
      +----> Database (SQLite/PostgreSQL)
      |
      +----> Transcript (structured, stored in DB)
      |
      +----> Call Outcome (classified enum)
      |
      +----> Dashboard (Streamlit)
      |
      +----> Daily Report (xlsx/csv)
```

**Why the conversation engine is split this way:** the compliance rules
forbid the AI from ever inventing an amount, date, or scheme detail. So
every number/date/scheme sentence is a Python string template filled
directly from the `ConsumerRecord` read off the sheet. An LLM (Anthropic
Claude) is used *only* to classify what the customer said into a fixed
intent enum and to detect language — never to generate financial content.
Its output is always parsed into a pydantic model (`CallDecision`) with
defensive guards (e.g. a promise-to-pay date is discarded if nothing in the
customer's actual words supports it) before it can touch the database.

**Why Google Cloud Speech instead of Twilio's built-in `<Say>`/`<Gather>`:**
Twilio's TTS (Amazon Polly) has no Urdu voice, and its default speech
recognition has weak Urdu coverage. Google Cloud Speech supports `ur-PK`
for both directions, streamed over Twilio Media Streams.

**Why "OpenClaw integration" is a skill, not embedded logic:** the OpenClaw
installed on this machine is a real personal multi-channel assistant
gateway (WhatsApp/Telegram-connected, Node/TS, with its own skills/cron/
plugins architecture) — not a telephony or voice-AI framework. So
`openclaw-skill/` is a thin CLI that calls this project's FastAPI campaign
control API (`app/campaign_control.py`), letting you say "start calling" or
"status" to your OpenClaw assistant. It is **not auto-installed** into your
live OpenClaw config; see [OpenClaw setup](#openclaw-setup).

## Prerequisites

- Python 3.12 or newer (tested on 3.14.5; the Docker image uses 3.12-slim)
- Node.js 18+ (only needed for the OpenClaw skill's CLI)
- A Google Cloud project with billing enabled (Sheets API is free; Speech
  API is pay-per-use)
- A Twilio account with a voice-capable phone number (or another
  telephony provider — only Twilio is implemented; see
  `app/telephony/base.py` to add another)
- An Anthropic API key (for the conversation engine's NLU classification)
- (Optional) OpenClaw installed, if you want chat-based campaign control

## Python installation

```bash
python3 --version   # must be 3.12+
```
If you don't have it: https://www.python.org/downloads/ (or your OS's
package manager — `apt install python3.12`, `brew install python@3.12`, etc.)

## OpenClaw setup

This project does not require OpenClaw to function — the FastAPI app,
dashboard, and webhooks all work standalone. OpenClaw only adds the
chat-based control layer (§40-41 in the build brief).

If you already have OpenClaw installed and want chat control:

```bash
openclaw skills install "/path/to/gsm-recovery-agent/openclaw-skill"
openclaw skills list        # confirm gsm-recovery-control shows up
openclaw skills info gsm-recovery-control
```

Then set (wherever OpenClaw runs skills):
```bash
export GSM_CONTROL_BASE_URL=http://127.0.0.1:8000   # or wherever app.main runs
export GSM_CONTROL_TOKEN=<same as CONTROL_API_TOKEN in .env, if set>
```

See `openclaw-skill/README.md` and `openclaw-skill/SKILL.md` for the full
command reference. If you don't have OpenClaw, skip this section entirely.

## Google Cloud project

1. Go to https://console.cloud.google.com/ and create a project (or reuse
   one).
2. Enable these APIs for the project (APIs & Services -> Library):
   - **Google Sheets API**
   - **Google Drive API** (needed for `gspread` to open a spreadsheet by ID)
   - **Cloud Speech-to-Text API**
   - **Cloud Text-to-Speech API**
3. Enable billing on the project (Speech APIs are not free-tier eligible
   at any real volume; Sheets/Drive API calls at this scale are free).

## Google Sheets API

No separate setup beyond enabling the API above — access is via the
service account created next.

## Service account

1. In Google Cloud Console: **IAM & Admin -> Service Accounts -> Create
   Service Account**.
2. Give it a name (e.g. `gsm-recovery-agent`). No project-level role is
   required (access is granted per-spreadsheet by sharing, see next
   section).
3. **Keys -> Add Key -> Create new key -> JSON**. Download the file.
4. Either:
   - Set `GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/that-file.json` in `.env`, or
   - Paste the file's entire JSON content as a single line into
     `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`.
5. For Speech APIs, the same service account works if you grant it the
   **Cloud Speech Client** / **Cloud Text-to-Speech User** roles (or just
   reuse the same credential — `GOOGLE_SPEECH_CREDENTIALS_JSON/FILE` falls
   back to `GOOGLE_SERVICE_ACCOUNT_JSON/FILE` if unset).

**Never commit this JSON file or paste it into `.env.example` or git.**
`.gitignore` already excludes `service-account*.json` and `*.key.json`.

## Sharing the Google Sheet

This project is built against GSM Brothers' **real, live sheet schema**
(not a hypothetical one) — the exact column headers already in use:

```
SNo, Contract, Contract Account, Consumer No., Meter No., CD, MRU, Name,
Address, Rate Tariff, DUE BCM, Total Due Units, Total Due Billing, LPD,
LPA, DUES, Scheme eligibility, Consumer Phone Number, IBC, Recovery Amount,
Remarks, Call Date, Call Time, Call Atempt, Call Status, Call out come,
Transcript, Recording URL, Promise to Pay, Date, Agent Notes
```
(the authoritative list is `app.schemas.ALL_SHEET_COLUMNS`; header text
must match exactly, including the real sheet's spelling of "Call Atempt").

The **minimum required** columns (the app refuses to run without these,
with a clear error naming exactly which is missing):
`Consumer No., Name, Consumer Phone Number, DUES, Call Status, Call Date`

Notable mapping decisions, made explicitly rather than guessed:
- **DUES** is the figure spoken to consumers as their outstanding balance
  — not "Total Due Billing" or "Recovery Amount", which are stored but
  never spoken on a call.
- There is **no dedicated Already Paid / Do Not Call / Human Follow-up
  column**. These are derived from text in `Call Status` / `Call out come`
  (e.g. a Call Status containing "Do Not Call" or "DNC", or a Call out come
  of exactly `ALREADY_PAID`/`DO_NOT_CALL`) — see `app/schemas.py`
  `_derive_already_paid` / `_derive_do_not_call` / `_derive_human_followup`.
  **The local `do_not_call` database table remains the authoritative
  compliance backstop** regardless of this text heuristic — once a
  consumer is recorded there (spec Sec.31, enforced immediately when a
  call ends with `do_not_call=true`), they are never queued again even if
  a sheet edit reverts the Call Status text.
- `CD`, `MRU`, `DUE BCM`, `LPD`, `LPA`, `IBC` are opaque K-Electric
  reference codes — stored and passed through untouched; no call-script or
  eligibility logic is built on their meaning.

Steps:
1. Confirm your sheet's row 1 has the headers above (any order, extra
   columns are fine).
2. Open the service account JSON file, copy the `client_email` value
   (looks like `gsm-recovery-agent@your-project.iam.gserviceaccount.com`).
3. In the Google Sheet, click **Share** and add that email as an **Editor**.
4. Copy the spreadsheet ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
5. Set `GOOGLE_SPREADSHEET_ID` and `GOOGLE_WORKSHEET_NAME` (the tab name)
   in `.env`.

The app never assumes column order — it looks up every field by header
name (`app/google_sheets.py`), and refuses to run against a sheet missing
any of the 6 required columns above (clear error, not a guess).

## Telephony provider setup

Only Twilio is implemented (`app/telephony/twilio_provider.py`), behind a
`TelephonyProvider` interface (`app/telephony/base.py`) so another provider
(Telnyx, etc.) can be added without touching call orchestration logic.

1. Create a Twilio account: https://www.twilio.com/try-twilio
2. Buy/reserve a **voice-capable** phone number (Console -> Phone Numbers).
   For Pakistani consumers, check Twilio's international calling
   permissions for Pakistan are enabled on your account (Console ->
   Voice -> Settings -> Geo Permissions).
3. From the Console dashboard, copy:
   - **Account SID** -> `TWILIO_ACCOUNT_SID`
   - **Auth Token** -> `TWILIO_AUTH_TOKEN`
   - Your purchased number (E.164, e.g. `+1...`) -> `TWILIO_PHONE_NUMBER`
4. Set `PUBLIC_BASE_URL` to a URL Twilio can reach for webhooks (see
   [Webhook configuration](#webhook-configuration)).
5. Set `CALL_RECORDING_ENABLED=true` only if you have a lawful basis and
   organizational approval to record calls in your jurisdiction — it
   defaults to `false`.

## AI provider setup

1. Get an API key from https://console.anthropic.com/
2. Set `AI_API_KEY` in `.env`. `AI_MODEL` defaults to `claude-sonnet-5`.
3. Without a key set, the conversation engine automatically falls back to
   an offline keyword classifier (`keyword_fallback_classifier` in
   `app/conversation_engine.py`) — this lets `TEST_MODE`/`DRY_RUN` work
   with zero API cost, but **it is not a substitute for the real LLM
   classifier in production**. Set `AI_API_KEY` before handling real calls.

## Environment variables

Copy `.env.example` to `.env` and fill in what you have:

```bash
cp .env.example .env
```

Every variable is documented inline in `.env.example`. The important
defaults to know:
- `TEST_MODE=true` and `DRY_RUN=true` out of the box — the system will
  not call a real consumer number or place a real telephony call until you
  deliberately change both.
- Missing credentials produce a clear `ConfigurationError` naming exactly
  which variable is missing, not a silent failure or a fake success.

## Local installation

```bash
git clone <this-repo>
cd gsm-recovery-agent      # i.e. this directory
./install.sh               # Linux/macOS: creates .venv, installs deps, runs tests
```

On Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
.venv\Scripts\python -m pytest tests/ -q
```

## Test mode

`TEST_MODE=true` (the default) guarantees the system can **never dial a
real consumer number** — every outbound call is redirected to
`TEST_PHONE_NUMBER`, and the `test-call` command uses the fixture
`TEST_CONSUMER_NAME`/`TEST_OUTSTANDING_AMOUNT`/`TEST_DUE_DATE`/
`TEST_SCHEME` values instead of a real sheet row.

Run the automated test suite (126 tests, no external credentials needed):
```bash
./.venv/bin/python -m pytest tests/ -v
```

## Dry-run mode

`DRY_RUN=true` (the default) skips telephony entirely and simulates a full
conversation in-process, so you can verify the whole pipeline —
Sheet → queue → schedule check → simulated call → DB write → simulated
Sheet write → report — for free, with no Twilio/Google Speech/Anthropic
account:

```bash
./.venv/bin/python scripts/dry_run_demo.py
```

This prints every step (see the script's output for a worked example with
three fixture consumers, one correctly excluded via a `Call Status` of
"Already Paid").

## Making a test call

Once you have real Twilio + Google Cloud Speech + Anthropic credentials
and want to place one actual phone call (to `TEST_PHONE_NUMBER`, never a
real consumer):

1. Set `TEST_MODE=true`, `DRY_RUN=false` in `.env`.
2. Set `TWILIO_*`, `AI_API_KEY`, `GOOGLE_SPEECH_CREDENTIALS_*` (or reuse
   the Sheets service account).
3. Set `PUBLIC_BASE_URL` to a Twilio-reachable URL (see next section).
4. Start the app: `python -m app.main`
5. Trigger the test call:
   ```bash
   curl -X POST http://127.0.0.1:8000/campaign/test-call
   ```
   (add `-H "X-Control-Token: ..."` if `CONTROL_API_TOKEN` is set)
6. Watch `logs/calls.log` and the Twilio Console's call log.

**This has not been run in the build environment** — no Twilio account
exists here. Verify it yourself before relying on it for real consumers.

## Webhook configuration

Twilio needs to reach your server over the public internet for
`/webhooks/voice/incoming`, `/status`, `/recording`, `/transcription`, and
the `/webhooks/voice/media-stream` WebSocket.

**Local development** — use a tunnel (ngrok shown, any similar tool works):
```bash
ngrok http 8000
# copy the https://xxxx.ngrok.io URL into PUBLIC_BASE_URL in .env
```

**Production** — point a real domain with a valid TLS certificate at the
app (see [Production deployment](#production-deployment)); set
`PUBLIC_BASE_URL=https://your-domain.example`.

Every webhook route validates the Twilio request signature
(`X-Twilio-Signature` header, HMAC against `TWILIO_AUTH_TOKEN`) before
trusting any field in the payload (`app/webhooks/voice.py:verify_and_extract`) —
this is genuinely enforced, not a placeholder; see
`tests/test_voice_webhooks.py::test_status_webhook_rejects_bad_signature`.

## Production deployment

1. Set `TEST_MODE=false` and `DRY_RUN=false` only after you've verified a
   real test call end-to-end (previous section) and reviewed the
   [compliance checklist](#compliance-considerations).
2. Use PostgreSQL, not SQLite, for concurrent-safe production use: set
   `DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/gsm_recovery`
   and `pip install psycopg2-binary` (not in `requirements.txt` by default
   since it needs no separate driver for the SQLite dev path).
3. Set `CONTROL_API_TOKEN` to a real secret — the campaign control API
   (start/pause/stop/test-call) is unauthenticated if this is unset.
4. Put the app behind a reverse proxy (nginx/Caddy) terminating TLS, and
   set `PUBLIC_BASE_URL` to the public HTTPS URL.
5. Run under a process supervisor (systemd unit, or `docker compose` — see
   below) so it restarts automatically; restart safety is built in
   (`app/scheduler.py:recover_stale_locks`, exercised by
   `tests/test_restart_recovery.py`).
6. Keep `MAX_CONCURRENT_CALLS=1` unless you've reviewed the
   duplicate-protection locking (`app/calling_agent.py:acquire_job_lock`)
   for your concurrency level — it's an atomic per-job lock, but the
   scheduler loop itself processes one consumer per tick sequentially.

## VPS deployment

```bash
ssh you@your-vps
git clone <this-repo> && cd gsm-recovery-agent
./install.sh
cp .env.example .env   # edit with real production values
sudo tee /etc/systemd/system/gsm-recovery.service <<'EOF'
[Unit]
Description=GSM Brothers Recovery Agent
After=network.target

[Service]
WorkingDirectory=/home/you/gsm-recovery-agent
ExecStart=/home/you/gsm-recovery-agent/.venv/bin/python -m app.main
Restart=always
RestartSec=5
EnvironmentFile=/home/you/gsm-recovery-agent/.env

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now gsm-recovery
sudo systemctl status gsm-recovery
```

## Docker deployment

```bash
cp .env.example .env    # edit with real values
docker compose up -d --build
docker compose logs -f app
```

This starts the FastAPI app (port 8000) and the dashboard (port 8501). For
PostgreSQL: `docker compose --profile postgres up -d --build` and set
`DATABASE_URL` in `.env` accordingly (see comments in `docker-compose.yml`).

**Not built/run in this environment** (no Docker daemon here) — run
`docker build .` yourself and confirm it succeeds before relying on it.

## Dashboard access

```bash
./.venv/bin/python -m streamlit run app/dashboard.py
```
Opens on http://localhost:8501 by default. Shows today's date/PK time,
scheduler state, campaign status, live activity, today's numbers, controls
(START/PAUSE/RESUME/STOP/RETRY FAILED/SYNC GOOGLE SHEET/TEST CALL/GENERATE
REPORT), and recent call outcomes. Reads the same database as `app.main`
directly — run it on the same machine/DB, or point both at the same
PostgreSQL instance in production.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ConfigurationError: Google Sheets integration is not configured...` | Set `GOOGLE_SERVICE_ACCOUNT_JSON`/`FILE` and `GOOGLE_SPREADSHEET_ID` |
| `SheetValidationError: Google Sheet is missing required column(s): ...` | Add the missing header(s) to row 1 of the sheet — exact text match |
| `403` on a webhook | Twilio signature didn't validate — check `PUBLIC_BASE_URL` exactly matches what Twilio calls (scheme + host + path + query), and that `TWILIO_AUTH_TOKEN` is correct |
| Calls never start | Check `/campaign/status` — is `scheduler_state` `CALLING`? Is `campaign_status` `RUNNING` (not `PAUSED`/`STOPPED`)? Both must be true |
| A consumer got called twice | Should not happen — file an issue with `logs/calls.log` around that consumer_no; check `call_events` table for the sequence. `MAX_CALL_RETRIES` bounds legitimate retries at 3 by default |
| Sheet not updating after calls | Check `call_attempts.sheet_synced`/`sheet_sync_error` in the DB — sheet failures never lose the DB result; run `POST /campaign/sync-sheet` to retry |
| Urdu audio sounds wrong / STT misses Urdu | Confirm `GOOGLE_SPEECH_CREDENTIALS_*` (or the Sheets service account) has Cloud Speech-to-Text/Text-to-Speech API access enabled and billing active |
| `pandas`/`pyarrow` fails to install on Windows | Use the versions pinned in `requirements.txt` (resolved to have prebuilt wheels for recent Python); don't downgrade them individually |

## Security

- **No credentials are ever hard-coded.** Every secret comes from
  environment variables (`.env`, gitignored).
- **Secrets are redacted from logs** (`app/logging_config.py:SecretMaskingFilter`)
  — Twilio auth token, AI API key, control API token, and Google service
  account JSON are stripped from every log line before it's written.
- **Every inbound webhook validates the Twilio signature** before trusting
  any field — see `app/webhooks/voice.py`.
- **The campaign control API supports a shared-secret token**
  (`CONTROL_API_TOKEN` / `X-Control-Token` header) — set it before exposing
  the control API beyond localhost.
- **`.gitignore` excludes** `.env`, `*.db`, service-account JSON files, and
  generated reports/logs.
- **Before deploying:** rotate any credential that was ever pasted into a
  chat, ticket, or shared document; restrict the Google service account to
  only the spreadsheets it needs (share-based access, no broader Drive
  scope); set `CONTROL_API_TOKEN`; put the app behind TLS.

## Compliance considerations

This system is designed as an AI-assisted recovery *calling* system, not a
deceptive or threatening one, per the build brief's explicit requirements:

- **Never invents data.** Outstanding amount, due date, consumer identity,
  scheme eligibility/details, surcharge information — all come directly
  from the authorized Google Sheet, via string templates
  (`app/conversation_engine.py`), never generated by the LLM. Tests assert
  this directly (`tests/test_conversation.py::test_dues_line_only_mentions_available_fields`,
  `test_scheme_line_never_invents_numbers_beyond_sheet_data`).
- **Identity verification before financial disclosure** — the greeting
  asks for identity confirmation; outstanding-amount information is only
  spoken after that stage passes (`ConversationEngine._handle_identity_reply`).
- **No threats, no false employee claims** — the scripted greeting
  identifies GSM Brothers and states the call is on behalf of K-Electric,
  never claims to *be* K-Electric staff (`app/conversation_engine.py:greeting_line`).
- **Do-not-call is honored immediately and persistently** — both on the
  sheet-sourced `Do Not Call` flag and a separate local `do_not_call`
  registry that survives a sheet re-import even if someone accidentally
  clears the sheet flag (`app/queue_manager.py:skip_reason`,
  `tests/test_queue.py::test_local_dnc_registry_overrides_sheet_even_if_sheet_flag_cleared`).
- **Human escalation is always available** — disputes, "already paid"
  claims, installment requests, and explicit human requests are all routed
  to `human_followup=True` rather than the AI attempting to resolve them
  unilaterally.
- **Retries are bounded** (`MAX_CALL_RETRIES`, default 3) — no indefinite
  re-dialing.
- **Recording is opt-in and off by default** (`CALL_RECORDING_ENABLED=false`).
  Confirm your own legal basis for call recording/monitoring under
  applicable Pakistani telecom, privacy, and consumer-protection law, and
  any requirements from K-Electric or PTA, before enabling it.
- **Telecom provider restrictions are not bypassed** — calls go through
  Twilio's standard voice API respecting its own compliance/permission
  gating for international calling to Pakistani numbers.

**This is guidance built into the code, not a legal opinion.** Confirm
with GSM Brothers' and K-Electric's legal/compliance teams that the exact
script wording, verification method, recording policy, and calling hours
satisfy PTA regulations and any consumer-protection obligations before
using this with real consumers.

### Compliance checklist before going live
- [ ] Legal sign-off on the greeting/verification/closing script wording
- [ ] Confirmed calling-hours compliance with applicable regulations
- [ ] Recording policy confirmed (enabled or not) with legal basis documented
- [ ] Do-not-call handling tested against the real Sheet
- [ ] Human escalation path (a real phone number/team) configured and staffed
- [ ] Data retention policy set for transcripts/recordings in the database

### Security checklist before going live
- [ ] `.env` never committed; real secrets rotated if ever exposed
- [ ] `CONTROL_API_TOKEN` set
- [ ] App served over HTTPS (`PUBLIC_BASE_URL` is `https://`)
- [ ] Google service account scoped to only the required spreadsheet(s)
- [ ] `CALL_RECORDING_ENABLED` matches your actual legal/compliance decision
- [ ] Log files reviewed to confirm no secret leakage (spot-check `logs/`)
- [ ] `DATABASE_URL` points at a properly access-controlled PostgreSQL
      instance in production, not a world-readable SQLite file

## Start commands (quick reference)

```bash
# Install
./install.sh

# Run the automated tests
./.venv/bin/python -m pytest tests/ -v

# Dry-run walkthrough (no credentials needed)
./.venv/bin/python scripts/dry_run_demo.py

# Start the app (FastAPI: webhooks + campaign control API + scheduler)
./.venv/bin/python -m app.main

# Start the dashboard
./.venv/bin/python -m streamlit run app/dashboard.py

# Docker
docker compose up -d --build

# OpenClaw skill (after `openclaw skills install openclaw-skill/`)
node openclaw-skill/cli.js status
```
