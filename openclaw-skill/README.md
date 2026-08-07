# gsm-recovery-control — OpenClaw skill

Controls the GSM Brothers AI Recovery Calling Agent from your OpenClaw
assistant (e.g. over WhatsApp/Telegram), by calling the recovery agent's
FastAPI campaign control API. See `SKILL.md` for the full command
reference.

This skill is **not auto-installed** into your live OpenClaw config — you
install it deliberately when you're ready:

```bash
openclaw skills install "D:\binyasin01\GSMB-AI-Agent\openclaw-skill"
```

(Or from within this directory: `openclaw skills install .`)

Then verify it's picked up:

```bash
openclaw skills list
openclaw skills info gsm-recovery-control
```

## Prerequisites

1. The recovery agent's FastAPI app must be running somewhere reachable
   from wherever OpenClaw executes skills:
   ```bash
   python -m app.main
   ```
2. If you set `CONTROL_API_TOKEN` in the recovery agent's `.env` (strongly
   recommended for anything beyond local dev), also set `GSM_CONTROL_TOKEN`
   in the environment OpenClaw runs skills in, to the same value.
3. If the recovery agent isn't on `http://127.0.0.1:8000`, set
   `GSM_CONTROL_BASE_URL` accordingly.

## Manual test

```bash
node cli.js status
```

With the recovery agent not running, this should print a JSON `error`
field (not hang or crash) — that's the expected, correct behavior; it
confirms the skill is wired up even before the agent is started.
