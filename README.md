# Conan Exiles Server Diagnostic Pipeline

Lightweight Python service for a Windows workstation hosting a Conan Exiles
dedicated server. When Uptime Kuma detects the server is down, this service
tails the Conan log, asks Claude what likely happened, and posts the
diagnosis to Discord.

```
Uptime Kuma → POST /kuma-webhook → tail ConanSandbox.log → Claude → Discord embed
```

## Setup

1. Clone this repo onto the box hosting the Conan server (e.g. `WINTOAD01`).
2. Install Python 3.11+.
3. Copy `.env.example` to `.env` and fill in your values:
   ```
   copy .env.example .env
   notepad .env
   ```
   Required: `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`.
   Optional overrides: `CONAN_LOG_PATH`, `LOG_TAIL_LINES`, `LISTENER_PORT`,
   `COOLDOWN_SECONDS`, `ANTHROPIC_MODEL`, `POST_RECOVERY_MESSAGES`.
4. Run it:
   ```
   run.bat
   ```
   The batch file creates `.venv` if it's missing, installs/updates
   requirements, then launches `diagnostics.py`. The Flask listener binds to
   `0.0.0.0:5555` by default.

   To run manually instead:
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python diagnostics.py
   ```

## Uptime Kuma configuration

In Kuma, create (or edit) a Notification of type **Webhook** pointing at:

```
http://<host>:5555/kuma-webhook
```

Use the default **Application/JSON** content type. Kuma's default body
includes a `heartbeat.status` field (`0` = down, `1` = up), which is exactly
what this service expects.

Attach the notification to your Conan monitor (TCP probe of the game port,
or a port-tester monitor against `7777/udp` upstream).

## Endpoints

| Method | Path             | Purpose                                  |
|--------|------------------|------------------------------------------|
| POST   | `/kuma-webhook`  | Receives Uptime Kuma webhooks            |
| GET    | `/health`        | Returns liveness + last event metadata   |

`/health` is safe for Kuma to monitor recursively if you want a heartbeat
on the diagnostic service itself.

## Behaviour notes

- **Cooldown**: Successive `status=0` webhooks within `COOLDOWN_SECONDS`
  (default 300s) are ignored to avoid spamming Discord during flapping.
  Cooldown does not apply to recovery (`status=1`) messages.
- **Recovery**: A green "back up" embed is posted on `status=1` if
  `POST_RECOVERY_MESSAGES=1`. No Claude call is made for recovery events.
- **Log path**: Defaults to `C:\ConanSaved\Logs\ConanSandbox.log`. If you
  are running the service from a different host, set `CONAN_LOG_PATH` to
  the UNC equivalent (e.g. `\\WINTOAD01\ConanSaved\Logs\ConanSandbox.log`).
- **Encoding**: The log is read as UTF-8 with `errors="replace"` to tolerate
  the byte garbage Unreal / BattlEye occasionally writes.
- **Graceful degradation**:
  - If the log cannot be read, the Discord embed reports the read error.
  - If the Claude call fails, the raw log tail is posted instead so the
    operator still has something actionable.
  - If Discord is unreachable, the error is logged locally and the
    listener stays up.
- **Local logs**: All events are written to `diagnostics.log` (rotated at
  1 MB, 3 backups) and mirrored to the console.

## Running as a Windows service (optional)

The simplest path is [nssm](https://nssm.cc/). Run `run.bat` once first so
`.venv` exists, then:

```powershell
nssm install ConanDiagnostics "C:\path\to\conan-server-diagnostics\.venv\Scripts\python.exe" "C:\path\to\conan-server-diagnostics\diagnostics.py"
nssm set ConanDiagnostics AppDirectory "C:\path\to\conan-server-diagnostics"
nssm set ConanDiagnostics AppStdout    "C:\path\to\conan-server-diagnostics\service.out.log"
nssm set ConanDiagnostics AppStderr    "C:\path\to\conan-server-diagnostics\service.err.log"
nssm start ConanDiagnostics
```

`.env` is read from the working directory, so make sure `AppDirectory`
points at the repo root.

## Manual testing

You don't need the Conan server to actually be down. Easiest options:

### 1. Curl the webhook directly

PowerShell with `curl.exe`:

```powershell
# Simulate "down" — triggers full pipeline (log tail → Claude → Discord)
curl.exe -X POST http://localhost:5555/kuma-webhook `
  -H "Content-Type: application/json" `
  -d '{\"heartbeat\":{\"status\":0},\"monitor\":{\"name\":\"Manual test\"}}'

# Simulate "up" — green recovery embed only, no Claude call
curl.exe -X POST http://localhost:5555/kuma-webhook `
  -H "Content-Type: application/json" `
  -d '{\"heartbeat\":{\"status\":1}}'

# Health check
curl.exe http://localhost:5555/health
```

Or pure PowerShell (no quoting headaches):

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:5555/kuma-webhook `
  -ContentType 'application/json' `
  -Body (@{ heartbeat = @{ status = 0 }; monitor = @{ name = 'Manual test' } } | ConvertTo-Json)
```

### 2. Use Kuma's built-in test button

In Kuma, go to *Settings → Notifications → (your webhook entry) → Test*.
Kuma fires a synthetic payload (`heartbeat: null, monitor: null,
msg: <notification name>`) at your endpoint. The service recognizes this
and posts a blue "Kuma webhook test" embed to Discord without invoking
Claude — so the Test button gives you visible end-to-end confirmation
that Kuma → service → Discord is wired up.

### Things to know while testing

- **Cooldown will bite you.** After one `status=0` test, the next
  `COOLDOWN_SECONDS` (default 300) of `status=0` requests return
  `{"action":"skipped_cooldown"}` and do nothing visible. Either wait, set
  `COOLDOWN_SECONDS=5` in `.env` for testing, or restart the service to
  reset the in-memory counter.
- **Force the no-log path.** Point `CONAN_LOG_PATH` at a path that doesn't
  exist and re-trigger — you should get a Discord embed reporting the
  read error rather than a crash.
- **Force the no-Claude path.** Temporarily blank out `ANTHROPIC_API_KEY`
  in `.env` and re-trigger — you should get the raw log tail in Discord
  with a fallback notice.
- **Watch `diagnostics.log`** (or the console) in another window while
  testing. Every webhook logs `Webhook received: status=...` and the
  resulting action.

## Troubleshooting

- **Kuma webhook always returns `unrecognized_payload`**: Kuma's payload
  shape varies between versions. Check the body Kuma is actually sending
  (`Settings → Notifications → Test`) and confirm it contains
  `heartbeat.status` or a top-level `status` field. Both shapes are
  accepted.
- **No Discord message on down**: Check `diagnostics.log` for either a
  cooldown skip line or a Discord HTTP error.
- **Claude returns garbage / refuses**: The default model in `.env.example`
  is `claude-sonnet-4-20250514`. Newer Claude models can be set via
  `ANTHROPIC_MODEL` if you want to upgrade.
