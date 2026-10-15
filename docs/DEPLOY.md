# Deploying Ragra on Railway

One container runs the dashboard and the reminder loop (`ragra serve-hosted`).
Data is SQLite on a persistent volume mounted at `/data`. Run exactly one
instance.

## Steps

1. Push this repo to GitHub.
2. Railway: New Project -> Deploy from GitHub repo -> select the repo. It
   builds the `Dockerfile` automatically.
3. Service -> Settings -> Volumes -> add a volume mounted at `/data`.
4. Service -> Settings -> Networking -> Generate Domain (gives an HTTPS URL,
   e.g. `https://ragra-production.up.railway.app`).
5. Service -> Variables, set:

| Variable | Value |
|---|---|
| `RAGRA_HOME` | `/data` |
| `RAGRA_TICK_INTERVAL_SECONDS` | `120` (minimum 30) |
| `RAGRA_TIMEZONE` | `Asia/Karachi` |
| `RAGRA_CREDENTIAL_KEY` | output of `python -m ragra.cli generate-credential-key` (keep a copy elsewhere) |
| `RAGRA_OAUTH_CLIENT_ID` / `RAGRA_OAUTH_CLIENT_SECRET` | Google OAuth web client |
| `RAGRA_OAUTH_REDIRECT_URI` | `https://<your-domain>/auth/callback` |
| `RAGRA_OWNER_EMAIL` | your Google email |
| `RAGRA_ALLOWED_EMAILS` | other allowed emails, comma-separated (optional) |
| `RAGRA_WEB_BASE_URL` | `https://<your-domain>` |
| `RAGRA_FAST_TIMETABLE_SPREADSHEET_ID`, `RAGRA_SHEETS_API_KEY` | see `.env.example` |
| `RAGRA_SMTP_HOST`, `RAGRA_SMTP_PORT`, `RAGRA_SMTP_USERNAME`, `RAGRA_SMTP_PASSWORD`, `RAGRA_EMAIL_FROM` | email sending (e.g. Gmail app password) |

6. Google Cloud Console -> APIs & Services -> Credentials -> your OAuth
   client: add `https://<your-domain>/auth/callback` and
   `https://<your-domain>/account/google/callback` to Authorized
   redirect URIs.
7. Open the URL, sign in, then connect Classroom and Calendar from the
   Account page.

## Known limits

- WhatsApp delivery goes through Hermes, which is not available on Railway.
  Until a hosted WhatsApp channel is built, hosted reminders go by email.
- Back up `/data/ragra.db` (Railway volume backups or a scheduled copy).
- Single instance only; moving to Postgres is what lifts that.
