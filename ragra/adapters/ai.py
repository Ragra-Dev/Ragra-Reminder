"""AI adapter: thin process-boundary wrapper around Hermes' existing
scripted one-shot mode (`hermes -z "<prompt>"` - no agent loop, no tool
access, just a single model completion). Same pattern as
ragra/adapters/notify.py: Ragra never imports Hermes' internal
provider/model-routing code, so it can't be broken by a Hermes upgrade and
can't be given more capability than "answer this one prompt."

This module is advisory-only by construction: it takes a prompt and returns
text. It never writes to Ragra's database. See ragra/ai/advisor.py for the
boundary that keeps deadlines/tasks/reminders deterministic and only ever
hands the AI a read-only snapshot.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class AIAdapterError(RuntimeError):
    """Raised when the AI adapter cannot produce a response."""


def ask(hermes_bin: Path | None, prompt: str, *, timeout_seconds: int = 90) -> str:
    if not hermes_bin:
        raise AIAdapterError("AI is not configured (HERMES_BIN not resolved)")

    try:
        proc = subprocess.run(
            [str(hermes_bin), "-z", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AIAdapterError(str(exc)) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        raise AIAdapterError(detail)

    return proc.stdout.strip()
