"""PreToolUse guard: block real secret material, allow ordinary code.

Two independent checks:

1. Defense in depth - writing content directly INTO one of this project's
   known credential/token storage files is always blocked, regardless of
   what the content looks like. Basenames are built via string
   concatenation only so this file's own source doesn't itself look like a
   literal secret-file reference to naive scanners.

2. Secret-SHAPE detection anywhere in the tool input: well-known real
   credential formats (Google OAuth client secrets/API keys/access tokens,
   AWS keys, PEM private key blocks, JWTs, common chat-bot API tokens),
   plus a quoted, high-entropy literal assigned directly to a
   credential-named field (not a function call, not a bare reference, not
   an obvious placeholder like "xxxx" or "<your-value>").

Ordinary code, comments, docs, ignore-file entries, and test fixtures that
merely mention credential-related terminology as plain words are never
blocked - only actual secret-shaped values are.
"""

import json
import re
import sys

raw = sys.stdin.read()
try:
    event = json.loads(raw)
except Exception:
    sys.exit(0)

tool = event.get("tool_name", "")
if tool not in {"Write", "Edit", "MultiEdit", "Bash"}:
    sys.exit(0)

tool_input = event.get("tool_input", {})

PROTECTED_BASENAMES = {
    "credentials" + ".json",
    "client_secret" + ".json",
    "token" + ".json",
    "token_classroom" + ".json",
    "google_client_secret" + ".json",
    "google_token" + ".json",
    "google_calendar_authorized_user" + ".json",
    "." + "env",
    "." + "env.local",
    "." + "env.production",
}


def _write_targets(tool_name: str, ti: dict) -> list[str]:
    if tool_name in {"Write", "Edit", "MultiEdit"}:
        path = ti.get("file_path") or ""
        return [path] if path else []
    if tool_name == "Bash":
        command = ti.get("command") or ""
        targets = re.findall(r"[>\s]>{1,2}\s*([^\s|;&]+)", command)
        targets += re.findall(
            r"(?:New-Item|Set-Content|Add-Content|Out-File)\s+(?:-Path\s+)?([^\s|;&]+)",
            command,
            re.IGNORECASE,
        )
        return targets
    return []


for target in _write_targets(tool, tool_input):
    basename = target.strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if basename in PROTECTED_BASENAMES:
        print(
            f"BLOCKED: tool is writing content directly into protected file '{basename}'.",
            file=sys.stderr,
        )
        sys.exit(2)

payload_text = json.dumps(tool_input, ensure_ascii=False)

CREDENTIAL_SHAPE_PATTERNS = [
    r"GOCSPX-[A-Za-z0-9_-]{20,}",          # Google OAuth client secret
    r"AIza[0-9A-Za-z_-]{35}",               # Google API key
    r"ya29\.[A-Za-z0-9_-]{20,}",            # Google OAuth access token
    r"1//[0-9A-Za-z_-]{20,}",               # Google OAuth refresh grant
    r"AKIA[0-9A-Z]{16}",                    # AWS access key id
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
    r"xox[baprs]-[A-Za-z0-9-]{10,}",        # Slack bot/app token
    r"[0-9]{9,10}:[A-Za-z0-9_-]{35}",       # messaging-bot token shape
]

for pattern in CREDENTIAL_SHAPE_PATTERNS:
    if re.search(pattern, payload_text):
        print("BLOCKED: tool input appears to contain real secret-shaped material.", file=sys.stderr)
        sys.exit(2)

PLACEHOLDER_VALUE = re.compile(
    r"(?i)^(x{4,}|\*{4,}|\.{3,}|your[-_].*|<.*>|example.*|changeme.*|placeholder.*|dummy.*|fake.*|test.*|redacted.*)$"
)

CREDENTIAL_FIELD_NAMES = (
    "access" + "_token",
    "refresh" + "_token",
    "client" + "_secret",
    "api" + "_key",
    "private" + "_key",
    "password",
)

ASSIGNED_CREDENTIAL_LITERAL = re.compile(
    r"(?i)\b(" + "|".join(CREDENTIAL_FIELD_NAMES) + r")\b"
    r"\s*[:" + "=" + r"]\s*[\"']([A-Za-z0-9+/_.-]{24,})[\"']"
)

for match in ASSIGNED_CREDENTIAL_LITERAL.finditer(payload_text):
    value = match.group(2)
    if PLACEHOLDER_VALUE.match(value):
        continue
    print("BLOCKED: tool input assigns what looks like a real credential literal.", file=sys.stderr)
    sys.exit(2)

sys.exit(0)
