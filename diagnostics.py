"""Conan Exiles server diagnostic pipeline.

Receives Uptime Kuma webhooks, tails the Conan server log, asks Claude what
went wrong, and posts a diagnosis to Discord.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
CONAN_LOG_PATH = os.getenv("CONAN_LOG_PATH", r"C:\ConanSaved\Logs\ConanSandbox.log")
LOG_TAIL_LINES = int(os.getenv("LOG_TAIL_LINES", "50"))
LISTENER_PORT = int(os.getenv("LISTENER_PORT", "5555"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
POST_RECOVERY_MESSAGES = os.getenv("POST_RECOVERY_MESSAGES", "1") == "1"

HOSTNAME = os.getenv("COMPUTERNAME") or socket.gethostname() or "WINTOAD01"

COLOR_DOWN = 16711680  # red
COLOR_RECOVERY = 65280  # green
COLOR_TEST = 3447003  # blue

DISCORD_EMBED_DESC_LIMIT = 4096
DIAGNOSIS_CHAR_LIMIT = 1800  # leaves headroom under the 4096 cap

SYSTEM_PROMPT = (
    "You are a Conan Exiles dedicated server diagnostic specialist. "
    "You will be given the tail of a ConanSandbox.log file from a Windows "
    "dedicated server that just went offline. "
    "Your job:\n"
    "1. Identify the most likely cause of the crash or shutdown.\n"
    "2. Quote the specific error/warning lines that support your conclusion "
    "(use backticks for inline quotes).\n"
    "3. Suggest a concrete remediation step the operator can try.\n"
    "If the log looks clean and no obvious cause is present, say so honestly "
    "and suggest where to look next (Windows event log, BattlEye, DLL crash "
    "dumps, etc.).\n"
    f"Keep the entire response under {DIAGNOSIS_CHAR_LIMIT} characters so it "
    "fits in a Discord embed. Use short paragraphs and bullet points where "
    "it improves readability."
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("conan-diagnostics")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)
log.addHandler(_console)

_file = RotatingFileHandler("diagnostics.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_file.setFormatter(_fmt)
log.addHandler(_file)


# ---------------------------------------------------------------------------
# Cooldown / state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_last_down_diagnosis_ts: float = 0.0
_last_event_ts: float | None = None
_last_event_kind: str | None = None


def _within_cooldown() -> bool:
    with _state_lock:
        return (time.monotonic() - _last_down_diagnosis_ts) < COOLDOWN_SECONDS


def _mark_diagnosis() -> None:
    global _last_down_diagnosis_ts
    with _state_lock:
        _last_down_diagnosis_ts = time.monotonic()


def _record_event(kind: str) -> None:
    global _last_event_ts, _last_event_kind
    with _state_lock:
        _last_event_ts = time.time()
        _last_event_kind = kind


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------

def tail_log(path: str, n: int) -> tuple[str | None, str | None]:
    """Return (tail_text, error_message). Exactly one will be non-None."""
    p = Path(path)
    if not p.exists():
        return None, f"Log file not found at `{path}`."
    try:
        # utf-8 with errors='replace' covers the assortment of byte garbage
        # that Unreal / BattlEye sometimes writes into logs.
        with p.open("r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=n)
        return "".join(tail).rstrip(), None
    except PermissionError as e:
        return None, f"Permission denied reading `{path}`: {e}"
    except OSError as e:
        return None, f"OS error reading `{path}`: {e}"
    except Exception as e:  # noqa: BLE001 - defensive: never crash the listener
        return None, f"Unexpected error reading `{path}`: {e}"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def diagnose_with_claude(log_tail: str) -> tuple[str | None, str | None]:
    """Return (diagnosis_text, error_message)."""
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY is not set."
    try:
        import anthropic
    except ImportError as e:
        return None, f"anthropic SDK not installed: {e}"

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Conan Exiles server `{HOSTNAME}` just went down. "
                        f"Here is the last {LOG_TAIL_LINES} lines of "
                        "ConanSandbox.log:\n\n"
                        f"```\n{log_tail}\n```\n\n"
                        "What likely happened and what should I try?"
                    ),
                }
            ],
        )
        parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        text = "\n".join(parts).strip()
        if not text:
            return None, "Claude returned an empty response."
        return text, None
    except Exception as e:  # noqa: BLE001 - SDK exposes several exception types
        return None, f"Anthropic API call failed: {e}"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _truncate_for_embed(text: str) -> str:
    if len(text) <= DISCORD_EMBED_DESC_LIMIT:
        return text
    suffix = "\n\n…(truncated to fit Discord 4096-char embed limit)"
    return text[: DISCORD_EMBED_DESC_LIMIT - len(suffix)] + suffix


def post_discord_embed(title: str, description: str, color: int, footer: str | None = None) -> bool:
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL is not set; cannot post embed: %s", title)
        return False
    embed: dict[str, Any] = {
        "title": title,
        "description": _truncate_for_embed(description),
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if footer:
        embed["footer"] = {"text": footer}
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=15,
        )
        if resp.status_code >= 300:
            log.error("Discord webhook returned %s: %s", resp.status_code, resp.text[:500])
            return False
        return True
    except requests.RequestException as e:
        log.error("Discord webhook post failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_down_pipeline() -> None:
    """Read log → ask Claude → post to Discord. Each step degrades gracefully."""
    log.info("Running down-pipeline for %s", HOSTNAME)

    tail_text, tail_err = tail_log(CONAN_LOG_PATH, LOG_TAIL_LINES)

    if tail_text is None:
        # Can't read log — report what we know and bail out of the Claude step.
        log.warning("Log tail unavailable: %s", tail_err)
        post_discord_embed(
            title=f"🔴 {HOSTNAME} Conan Server Down",
            description=(
                "Server reported down by Uptime Kuma, but I could not read "
                f"the log:\n\n```\n{tail_err}\n```"
            ),
            color=COLOR_DOWN,
            footer=f"Diagnosed by Claude | {ANTHROPIC_MODEL}",
        )
        return

    diagnosis, diag_err = diagnose_with_claude(tail_text)

    if diagnosis is None:
        log.warning("Claude diagnosis failed: %s", diag_err)
        # Fall back to the raw log tail so the operator still has something
        # actionable in Discord.
        fallback = (
            f"⚠️ Automated diagnosis failed: `{diag_err}`\n\n"
            f"Raw tail of `{CONAN_LOG_PATH}`:\n```\n{tail_text}\n```"
        )
        post_discord_embed(
            title=f"🔴 {HOSTNAME} Conan Server Down",
            description=fallback,
            color=COLOR_DOWN,
            footer=f"Diagnosed by Claude | {ANTHROPIC_MODEL} (fallback)",
        )
        return

    post_discord_embed(
        title=f"🔴 {HOSTNAME} Conan Server Down",
        description=diagnosis,
        color=COLOR_DOWN,
        footer=f"Diagnosed by Claude | {ANTHROPIC_MODEL}",
    )


def run_recovery_pipeline() -> None:
    if not POST_RECOVERY_MESSAGES:
        return
    log.info("Posting recovery message for %s", HOSTNAME)
    post_discord_embed(
        title=f"🟢 {HOSTNAME} Conan Server Back Up",
        description="Uptime Kuma reports the server is responding again.",
        color=COLOR_RECOVERY,
        footer="Recovery notice",
    )


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


def _extract_status(payload: dict[str, Any]) -> int | None:
    """Uptime Kuma sends `{"heartbeat": {"status": 0|1, ...}, "monitor": {...}, ...}`.

    Be liberal in what we accept — some templated webhooks flatten the shape.
    """
    if not isinstance(payload, dict):
        return None
    hb = payload.get("heartbeat")
    if isinstance(hb, dict) and "status" in hb:
        try:
            return int(hb["status"])
        except (TypeError, ValueError):
            return None
    if "status" in payload:
        try:
            return int(payload["status"])
        except (TypeError, ValueError):
            return None
    return None


def _is_kuma_test_payload(payload: dict[str, Any]) -> bool:
    """Kuma's notification "Test" button sends heartbeat=null and monitor=null."""
    if not isinstance(payload, dict):
        return False
    has_kuma_shape = "heartbeat" in payload and "monitor" in payload
    return has_kuma_shape and payload.get("heartbeat") is None and payload.get("monitor") is None


@app.post("/kuma-webhook")
def kuma_webhook():
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:  # noqa: BLE001
        payload = {}

    if _is_kuma_test_payload(payload):
        msg = str(payload.get("msg") or "Test")
        log.info("Kuma test webhook received: %s", msg)
        _record_event("test")
        post_discord_embed(
            title=f"🔵 {HOSTNAME} Kuma webhook test",
            description=(
                f"Received Kuma notification test: `{msg}`.\n\n"
                "End-to-end chain is wired up. Real outages will trigger "
                "the full diagnostic pipeline."
            ),
            color=COLOR_TEST,
            footer="Test event (no Claude call)",
        )
        return jsonify({"ok": True, "action": "test_acknowledged"}), 200

    status = _extract_status(payload)
    log.info("Webhook received: status=%s payload_keys=%s", status, list(payload.keys()))

    if status == 0:
        _record_event("down")
        if _within_cooldown():
            log.info("Within %ss cooldown; skipping diagnosis.", COOLDOWN_SECONDS)
            return jsonify({"ok": True, "action": "skipped_cooldown"}), 200
        _mark_diagnosis()
        threading.Thread(target=run_down_pipeline, name="down-pipeline", daemon=True).start()
        return jsonify({"ok": True, "action": "diagnosing"}), 202

    if status == 1:
        _record_event("up")
        threading.Thread(target=run_recovery_pipeline, name="recovery-pipeline", daemon=True).start()
        return jsonify({"ok": True, "action": "recovery"}), 202

    log.warning("Unrecognized webhook payload; ignoring. Raw: %s", str(payload)[:500])
    return jsonify({"ok": False, "reason": "unrecognized_payload"}), 200


@app.get("/health")
def health():
    with _state_lock:
        return jsonify(
            {
                "ok": True,
                "hostname": HOSTNAME,
                "last_event_ts": _last_event_ts,
                "last_event_kind": _last_event_kind,
                "cooldown_seconds": COOLDOWN_SECONDS,
                "model": ANTHROPIC_MODEL,
                "log_path": CONAN_LOG_PATH,
            }
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    log.info(
        "Starting Conan diagnostics listener on 0.0.0.0:%s (cooldown=%ss, model=%s, log=%s)",
        LISTENER_PORT,
        COOLDOWN_SECONDS,
        ANTHROPIC_MODEL,
        CONAN_LOG_PATH,
    )
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — diagnoses will fall back to raw log tail.")
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — embeds will not be delivered.")
    app.run(host="0.0.0.0", port=LISTENER_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
