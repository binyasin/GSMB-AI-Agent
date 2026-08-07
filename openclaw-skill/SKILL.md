---
name: gsm-recovery-control
description: Control the GSM Brothers AI Recovery Calling Agent (K-Electric consumer recovery calls) — start/pause/resume/stop the daily calling campaign, check status, view the next consumer in queue, generate the daily report, retry failed calls, sync the Google Sheet, or place a test call.
metadata: {"clawdbot":{"emoji":"📞","requires":{"bins":["node"]},"install":[{"id":"npm","kind":"npm","cwd":"__skill__","label":"Install dependencies (none required — uses Node's built-in fetch)"}]}}
---

# GSM Recovery Control

Lets you operate the GSM Brothers AI Recovery Calling Agent — the system
that calls K-Electric consumers about outstanding dues — from chat,
instead of needing the dashboard open. This skill only talks to the
campaign control API; it never places calls or touches consumer data
itself.

**Requires the recovery agent's FastAPI app to be running** (`python -m
app.main` in the `gsm-recovery-agent` project). If it isn't reachable, every
command below returns a clear JSON error instead of hanging.

## What the user might say (intent -> command)

| User says | Run |
|---|---|
| "start calling" / "start the recovery campaign" | `node <SKILL_DIR>/cli.js start` |
| "pause calling" | `node <SKILL_DIR>/cli.js pause` |
| "resume calling" | `node <SKILL_DIR>/cli.js resume` |
| "stop calling" / "stop the campaign" | `node <SKILL_DIR>/cli.js stop` |
| "status" / "how's the campaign going" / "is it calling right now" | `node <SKILL_DIR>/cli.js status` |
| "next consumer" / "who's next" | `node <SKILL_DIR>/cli.js next-consumer` |
| "today's report" / "generate the daily report" | `node <SKILL_DIR>/cli.js report` |
| "report for <date>" | `node <SKILL_DIR>/cli.js report --day YYYY-MM-DD` |
| "retry failed calls" | `node <SKILL_DIR>/cli.js retry-failed` |
| "sync google sheet" / "sync the sheet" | `node <SKILL_DIR>/cli.js sync-sheet` |
| "test call" / "place a test call" | `node <SKILL_DIR>/cli.js test-call` |

`<SKILL_DIR>` is this skill's install directory.

## Commands

All commands print one JSON object to stdout and exit non-zero on failure.

```bash
node <SKILL_DIR>/cli.js status
```
```json
{
  "now": "2026-08-07T09:35:00+05:00",
  "scheduler_state": "CALLING",
  "campaign_status": "RUNNING",
  "current_consumer_no": "CN-00123",
  "consumers_remaining_in_queue": 41,
  "today": { "calls_attempted": 12, "calls_completed": 9, "...": "..." }
}
```

- `scheduler_state` — what the automatic daily schedule says right now:
  `WAITING`, `CALLING`, `BREAK`, `DAY_COMPLETED`, or `WAITING_FOR_NEXT_DAY`.
- `campaign_status` — the operator override on top of the schedule:
  `RUNNING`, `PAUSED`, or `STOPPED`. A call is only placed when the
  schedule says `CALLING` **and** campaign_status is `RUNNING`.

```bash
node <SKILL_DIR>/cli.js next-consumer
```
Returns the next consumer that would be dialed, without dialing them —
useful for "who's next" without side effects.

```bash
node <SKILL_DIR>/cli.js report [--day 2026-08-07]
```
Generates (or regenerates) `daily_report_<date>.xlsx`/`.csv` under the
server's `reports/` directory and returns their paths. Defaults to today.

```bash
node <SKILL_DIR>/cli.js retry-failed
```
Resets today's NO_ANSWER/BUSY/FAILED jobs that haven't exhausted
MAX_CALL_RETRIES back to PENDING so they're picked up on the next tick.

```bash
node <SKILL_DIR>/cli.js sync-sheet
```
Retries writing any call results that failed to sync to Google Sheets
earlier (e.g. during a Sheets API outage). Returns `synced_count`.

```bash
node <SKILL_DIR>/cli.js test-call
```
Places one call to `TEST_PHONE_NUMBER` using the fixture consumer data
(`TEST_CONSUMER_NAME`, `TEST_OUTSTANDING_AMOUNT`, etc.), bypassing the real
queue entirely. Fails with a clear error if `TEST_MODE` is not enabled on
the server — this command can never target a real consumer number.

## Configuration

No secrets are stored in this skill — it's a thin HTTP client. Set these
env vars where OpenClaw runs the skill (or export them in your shell
profile):

- `GSM_CONTROL_BASE_URL` — where the recovery agent's FastAPI app is
  reachable. Default: `http://127.0.0.1:8000`.
- `GSM_CONTROL_TOKEN` — only needed if the server has `CONTROL_API_TOKEN`
  set (recommended for anything beyond localhost). Sent as the
  `X-Control-Token` header.

## Reporting results to the user

- After `start`/`pause`/`resume`/`stop`, confirm the new `campaign_status`
  in plain language ("Campaign is now paused — no new calls will start
  until you resume it").
- For `status`, summarize the key numbers (calls attempted/completed today,
  consumers remaining) rather than dumping the raw JSON.
- If a command returns an `error` field, surface it directly — most errors
  are actionable (e.g. "recovery agent isn't running", "TEST_MODE is
  disabled", "Google Sheets not configured").
