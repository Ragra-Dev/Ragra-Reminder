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
| `RAGRA_OPEN_SIGNUP` | `1` to let anyone with a verified Google email join (off by default) |
| `RAGRA_MAX_USERS` | optional cap on accounts open sign-up may create, e.g. `100` |
| `RAGRA_FAST_TIMETABLE_SPREADSHEET_ID`, `RAGRA_SHEETS_API_KEY` | see `.env.example` |
| `RAGRA_SMTP_HOST`, `RAGRA_SMTP_PORT`, `RAGRA_SMTP_USERNAME`, `RAGRA_SMTP_PASSWORD`, `RAGRA_EMAIL_FROM` | email sending (e.g. Gmail app password) |

6. Google Cloud Console -> APIs & Services -> Credentials -> your OAuth
   client: add `https://<your-domain>/auth/callback` and
   `https://<your-domain>/account/google/callback` to Authorized
   redirect URIs.
7. Open the URL, sign in, then connect Classroom and Calendar from the
   Account page.

## Telegram alerts and chat (optional, free, recommended)

The simplest hosted chat channel: no business account, no approvals, no
24-hour window. The bot receives messages through a webhook that Ragra
registers by itself when the server starts.

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username
   ending in `bot`. It replies with a token.
2. Railway Variables:

| Variable | Value |
|---|---|
| `RAGRA_TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `RAGRA_TELEGRAM_BOT_USERNAME` | the bot's username, with or without `@` |
| `RAGRA_WEB_BASE_URL` | `https://<your-domain>` (must be https; already set for email links) |
| `RAGRA_GEMINI_API_KEY` | optional, free key from https://aistudio.google.com/apikey for free-form questions |

3. Wait for the redeploy. The log shows `telegram: webhook registered`.
   (`python -m ragra.cli telegram-set-webhook` does the same by hand.)
4. In Ragra: Account -> Notifications -> **Connect Telegram**, then press
   **Start** in the chat that opens. Ragra links that chat to your account and
   replies. Ask `pending`, `today`, `overdue`, `week`, `news`, or any question.

Alerts for new announcements, assignments, materials and deadline changes then
arrive in that chat, filtered to your section. The link code is single-use and
expires after 30 minutes; the chat id is only ever set by that handshake.

## WhatsApp alerts and chat (optional)

Note: a brand-new, unverified Meta business account can be locked by Meta
(error 131031 "Business Account locked"), after which nothing is delivered.
Telegram above avoids that.

Ragra can send alerts to WhatsApp and answer questions there ("pending",
"today", "overdue", or a free question) through Meta's WhatsApp Cloud API. It
works on Railway because it is plain HTTPS in both directions.

1. https://developers.facebook.com -> Create app (type Business) -> add the
   **WhatsApp** product. Meta gives a free test number.
2. WhatsApp -> API Setup: copy the **Phone number ID**, and under "To" add your
   own phone as a recipient (the test number can only message verified numbers).
3. Create a permanent access token (Business settings -> System users -> add one,
   assign the app, generate a token with `whatsapp_business_messaging`). The
   24-hour token on the API Setup page also works for a first test.
4. App settings -> Basic: copy the **App secret**.
5. Railway Variables:

| Variable | Value |
|---|---|
| `RAGRA_WHATSAPP_TOKEN` | the access token |
| `RAGRA_WHATSAPP_PHONE_NUMBER_ID` | the Phone number ID |
| `RAGRA_WHATSAPP_APP_SECRET` | the App secret (requests are rejected without it) |
| `RAGRA_WHATSAPP_VERIFY_TOKEN` | any long random string you invent |
| `RAGRA_WHATSAPP_DISPLAY_NUMBER` | optional, the bot's number as shown on the Account page |
| `RAGRA_WHATSAPP_TEMPLATE` | optional, an approved template with one body variable, used for alerts after the 24-hour window closes |
| `RAGRA_GEMINI_API_KEY` | free key from https://aistudio.google.com/apikey - enables free-form questions |
| `RAGRA_GEMINI_MODEL` | optional, defaults to `gemini-2.5-flash` |

6. WhatsApp -> Configuration -> Webhook: callback URL
   `https://<your-domain>/whatsapp/webhook`, verify token = the value of
   `RAGRA_WHATSAPP_VERIFY_TOKEN`, then Verify and save and subscribe to the
   **messages** field.
7. In Ragra: Account -> Notifications -> tick WhatsApp, enter your number, save.
   Then send `hi` to the bot number from WhatsApp.

Meta only allows free-form messages within 24 hours of the person's last message
to the bot. Alerts sent after that window need an approved template
(`RAGRA_WHATSAPP_TEMPLATE`); without one they fail and are retried, and they
reach the person the next time they message the bot. Email is unaffected.

New Classroom announcements (posted within the last 3 days when first seen) are
sent to every enabled channel, the same way deadline reminders are. Anything the
section filter judges to be for another section is not sent and is not shown in
chat answers.

## Known limits

- The Hermes WhatsApp option does not work on Railway (Hermes is not in the
  container); use the Cloud API channel above instead.
- Back up `/data/ragra.db` (Railway volume backups or a scheduled copy).
- Single instance only; moving to Postgres is what lifts that.
