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
2. Install Python 3.11+ and create a venv:
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your values:
   ```
   copy .env.example .env
   notepad .env
   ```
   Required: `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`.
   Optional overrides: `CONAN_LOG_PATH`, `LOG_TAIL_LINES`, `LISTENER_PORT`,
   `COOLDOWN_SECONDS`, `ANTHROPIC_MODEL`, `POST_RECOVERY_MESSAGES`.
4. Run it:
   ```powershell
   python diagnostics.py
   ```
   The Flask listener binds to `0.0.0.0:5555` by default.

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

The simplest path is [nssm](https://nssm.cc/):

```powershell
nssm install ConanDiagnostics "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\diagnostics.py"
nssm set ConanDiagnostics AppDirectory "C:\path\to\conan-server-diagnostics"
nssm set ConanDiagnostics AppStdout    "C:\path\to\conan-server-diagnostics\service.out.log"
nssm set ConanDiagnostics AppStderr    "C:\path\to\conan-server-diagnostics\service.err.log"
nssm start ConanDiagnostics
```

`.env` is read from the working directory, so make sure `AppDirectory`
points at the repo root.

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
