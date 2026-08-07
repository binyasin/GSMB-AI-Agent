#!/usr/bin/env node
/**
 * GSM Brothers AI Recovery Calling Agent — OpenClaw skill CLI.
 *
 * Thin wrapper around the campaign control FastAPI (app/campaign_control.py).
 * Every command prints a single JSON object to stdout and exits 0 on
 * success, non-zero on failure — matching the convention OpenClaw skills
 * use so the host LLM can parse the result directly.
 *
 * Config (env vars, no config file — no secrets live in this skill dir):
 *   GSM_CONTROL_BASE_URL   default: http://127.0.0.1:8000
 *   GSM_CONTROL_TOKEN      optional, sent as X-Control-Token (required if
 *                          the server has CONTROL_API_TOKEN set)
 */

const BASE_URL = process.env.GSM_CONTROL_BASE_URL || "http://127.0.0.1:8000";
const TOKEN = process.env.GSM_CONTROL_TOKEN || "";

const COMMANDS = {
  "start": { method: "POST", path: "/campaign/start" },
  "pause": { method: "POST", path: "/campaign/pause" },
  "resume": { method: "POST", path: "/campaign/resume" },
  "stop": { method: "POST", path: "/campaign/stop" },
  "status": { method: "GET", path: "/campaign/status" },
  "next-consumer": { method: "GET", path: "/campaign/next-consumer" },
  "report": { method: "POST", path: "/campaign/report" },
  "retry-failed": { method: "POST", path: "/campaign/retry-failed" },
  "sync-sheet": { method: "POST", path: "/campaign/sync-sheet" },
  "test-call": { method: "POST", path: "/campaign/test-call" },
};

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        args[key] = next;
        i++;
      } else {
        args[key] = true;
      }
    } else {
      args._.push(token);
    }
  }
  return args;
}

async function main() {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const args = parseArgs(argv.slice(1));

  if (!command || !COMMANDS[command]) {
    console.log(
      JSON.stringify({
        error: `unknown command '${command || ""}'`,
        available_commands: Object.keys(COMMANDS),
      })
    );
    process.exit(1);
  }

  const { method, path } = COMMANDS[command];
  let url = `${BASE_URL}${path}`;
  if (command === "report" && args.day) {
    url += `?day=${encodeURIComponent(args.day)}`;
  }

  const headers = {};
  if (TOKEN) headers["X-Control-Token"] = TOKEN;

  try {
    const response = await fetch(url, { method, headers });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      console.log(JSON.stringify({ error: body.detail || response.statusText, status: response.status }));
      process.exit(1);
    }
    console.log(JSON.stringify(body));
  } catch (err) {
    console.log(
      JSON.stringify({
        error: `could not reach the GSM recovery agent at ${BASE_URL}: ${err.message}`,
        hint: "Is the FastAPI app running (python -m app.main)? Set GSM_CONTROL_BASE_URL if it's on a different host/port.",
      })
    );
    process.exit(1);
  }
}

main();
