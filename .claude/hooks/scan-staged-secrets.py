import re, subprocess, sys

try:
    diff = subprocess.check_output(
        ["git", "diff", "--cached", "--no-ext-diff", "--unified=0"],
        text=True, stderr=subprocess.DEVNULL
    )
except Exception:
    sys.exit(0)

patterns = [
    (r"(?i)-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key"),
    (r"(?i)AIza[0-9A-Za-z\-_]{20,}", "Google API key"),
    (r"(?i)gh[pousr]_[A-Za-z0-9_]{20,}", "GitHub token"),
    (r"(?i)sk-[A-Za-z0-9_\-]{20,}", "API key"),
    (r"(?i)(client_secret|refresh_token|access_token)\s*[:=]\s*['\"][^'\"]+", "OAuth token/secret"),
]

for pattern, label in patterns:
    if re.search(pattern, diff):
        print(f"BLOCKED: staged diff appears to contain {label}.", file=sys.stderr)
        sys.exit(2)

for f in ["credentials.json", "client_secret.json", "token.json", ".env", ".env.local", ".env.production"]:
    if re.search(r"(?m)^\+\+\+ b/" + re.escape(f) + r"$", diff):
        print(f"BLOCKED: protected file staged: {f}", file=sys.stderr)
        sys.exit(2)

sys.exit(0)
