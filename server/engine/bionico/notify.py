"""Operator alerts via ntfy.sh and/or Telegram. Silent no-op when unconfigured.

Configuration is read from the environment first, then from the repo's `.env`:

  NTFY_TOPIC           -> POST https://ntfy.sh/<topic>
  TELEGRAM_BOT_TOKEN
  + TELEGRAM_CHAT_ID   -> Telegram Bot API sendMessage
  BIONICO_NOTIFY=0     -> disable notifications entirely (used by tests)

`notify()` never raises and never blocks the caller: the HTTP sends run on a
daemon thread with short timeouts, and every failure is swallowed. Alerts are
operator-facing convenience only; losing one must never affect a job.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request

_TIMEOUT_SECONDS = 10
_USER_AGENT = "bionico-notify/1.0"


def _setting(name: str) -> str | None:
    """Env var first, then the repo `.env` (where the install writes secrets)."""
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    try:
        from bionico import config

        cfg = config.load()
        val = (config.load_env(cfg.env_path).get(name) or "").strip()
        return val or None
    except Exception:
        return None


def _send_ntfy(topic: str, title: str, message: str) -> None:
    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic, safe=""),
        data=("%s\n%s" % (title, message)).encode("utf-8"),
        method="POST",
        headers={"User-Agent": _USER_AGENT,
                 "Content-Type": "text/plain; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS):
        pass


def _send_telegram(token: str, chat_id: str, title: str, message: str) -> None:
    payload = json.dumps(
        {"chat_id": chat_id, "text": "%s\n%s" % (title, message)}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=payload,
        method="POST",
        headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS):
        pass


def _send_all(targets, title: str, message: str) -> None:
    for fn in targets:
        try:
            fn(title, message)
        except Exception:
            pass  # best-effort: one broken channel must not affect the other


def notify(title: str, message: str) -> None:
    """Fire-and-forget operator alert. Never raises; no-op when unconfigured."""
    try:
        if (os.environ.get("BIONICO_NOTIFY") or "").strip() == "0":
            return
        targets = []
        topic = _setting("NTFY_TOPIC")
        if topic:
            targets.append(lambda t, m, _topic=topic: _send_ntfy(_topic, t, m))
        token = _setting("TELEGRAM_BOT_TOKEN")
        chat_id = _setting("TELEGRAM_CHAT_ID")
        if token and chat_id:
            targets.append(
                lambda t, m, _tok=token, _chat=chat_id: _send_telegram(_tok, _chat, t, m)
            )
        if not targets:
            return
        threading.Thread(
            target=_send_all, args=(targets, str(title), str(message)), daemon=True
        ).start()
    except Exception:
        pass
