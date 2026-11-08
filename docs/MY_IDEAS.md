# My ideas and where I left off

Read this first when resuming work. Update it at the end of every session.
Last updated: 2026-11-08

## Where things stand (2026-11-08)
Everything below is committed and pushed to GitHub (2026-11-08).
- **Pages** (all share the blue sidebar / phone bottom bar look): Home (chat card + upcoming deadline +
  quick access), Chat, Tasks, Deadlines, Announcements, Missed, Notifications, Profile, Contact us.
- **Chat**: typing dots, then the answer is written live. On phones `/chat` opens full screen like
  WhatsApp and follows the keyboard (`visualViewport`). Unread badge counts only Ragra's alerts.
- **Deadlines**: grouped list with filters (All, groups, within 24 hours / 7 days / 1 week) and a red
  "due within 3 hours" warning. Tapping one opens one clean detail page (`/deadlines/{id}`; `/tasks/{id}`
  shows the same page). Sidebar highlights Deadlines for the first, nothing for the second.
- **Google Classroom permissions**: Ragra asks Google only for read-only Classroom scopes (Calendar is the
  one allowed extra). Profile has one merged Permissions list: green tick = allowed, red cross = tap to
  allow (opens Google's permission page), plus a Classroom information box (classes as teacher/student).
  If a coursework permission is refused for a teacher account the note says so.
- **WhatsApp / Telegram** are shown as "Under development" and disabled. Telegram code is kept (not deleted).
- **Contact us**: two team cards (Aoun Abbas yellow background, Hashim Amjid blue, round photos with email
  links) and a feedback form (type, stars, message). Feedback is saved in the `feedback` table (migration
  0034). The owner (`RAGRA_OWNER_EMAIL`) reads everything at `/contact/inbox`.

### Latest work (2026-11-08)
- **Feedback is emailed to both team members** after it is saved (`ragra/feedback_mail.py`, migration 0035).
  Uses Brevo (`RAGRA_BREVO_API_KEY` + `RAGRA_EMAIL_FROM`) or SMTP; `RAGRA_FEEDBACK_TO` overrides the
  recipients. The inbox shows "Sent to 2 of 2" or the reason it failed. Brevo itself works now.
- **Hosting move**: Railway's free trial ends after 7 days, so the plan is **Oracle Cloud Always Free**
  (AMD micro VM `VM.Standard.E2.1.Micro`, Ubuntu 22.04, Docker Compose + Caddy + a free DuckDNS name).
  Why: always on (reminder loop), real disk for the SQLite file, one process, outbound HTTPS. Render free
  sleeps and has no disk; Koyeb has no free plan; Fly is trial only. Full steps: `docs/DEPLOY-ORACLE.md`;
  daily backup script: `scripts/backup-db.sh`; settings template: `deploy.env.example`.
  Watch-outs: needs a card for sign-up, Oracle may reclaim an idle free VM (upgrade to Pay As You Go if
  warned), keep a private copy of `RAGRA_CREDENTIAL_KEY`, add the new redirect URIs in Google Cloud.

## The idea
Get live messages from Ragra when Google Classroom (GCR) has something new:
announcements, materials, quiz-related queries. It should work like WhatsApp:
a bot called "Ragra" sends a message and the phone gets a notification even
when the app is closed. I should also be able to ask questions back (quiz,
announcements) in the same chat.

Two ways I wanted:
1. A bot on WhatsApp (Meta Cloud API, one bot number).
2. A chat inside the Ragra app that delivers messages while the app is closed.

## Constraints
- Must be free of cost.
- Was deployed on Railway (HTTPS URL exists), but its free period is only 7 days, so moving to Oracle Cloud Always Free (see above).
- Telegram is blocked in Pakistan, so it is NOT usable for me. Don't build more on it.
- Student in Pakistan (FAST-NUCES).

## Direction chosen (2026-11-06)
Going with the **in-app chat bot** ("Ragra" in `/chat`) as the main path. WhatsApp and Telegram are secondary/optional.

## Branding (chosen 2026-11-06)
- Logo: the **"R" with a cyan checkmark and an orange dot**, white on a blue rounded tile (app icon). Final version = the image the user supplied on 2026-11-06; PNG sizes live in `ragra/web/static/icons/` (512, 192, 180 apple-touch, 32 favicon, 96 in-app). Tagline: "Never Miss What's Next."
- Web Push build was started (design reviewed: provider in `ragra/adapters/notify.py`, `push_subscriptions` migration 0034, service worker at `/sw.js`, manifest, `/account` enable button) but NO push code is written yet - icons are ready for it.
- Colors: #2563EB primary, #60A5FA secondary, #FBBF24 accent, #0F172A text, #F1F5FF background. Font: Plus Jakarta Sans.
- Done: logo in the top bar, landing page, and browser tab icon (`ragra/web/templates/_logo.html`).
- Still to do: use the Ragra Bot mascot as the chat avatar, load Plus Jakarta Sans, PNG app icons (192/512) for the PWA, monochrome notification icon.

## UI redesign (2026-11-06, uncommitted)
- Home page (`/`) is an exact copy of my two mockups (phone + desktop): greeting + robot,
  Ragra Bot chat card, Upcoming Deadline (nearest task, priority from due date), Quick Access.
  Template: `ragra/web/templates/home.html`. Robot, logo mark and bot avatar were cut from the
  mockup into `ragra/web/static/icons/` (robot.png, logo-mark.png, bot-avatar.png) - replace with
  the original artwork files when I have them for sharper edges.
- The old home page moved to `/overview` ("Stats" in the bottom bar). Nothing was deleted.
- All other pages share the look via `ragra/web/templates/_shell.html`: blue desktop sidebar
  scaled from the desktop mockup, phone header (logo, bell, avatar) and bottom bar
  (Home, Tasks, Stats, Profile).
- Chat unread badge counts only Ragra's alerts; the welcome message now also lands in the chat.
- "Categories" tile opens Announcements for now (no categories feature yet).
- Don't delete anything (including the Telegram code) - my rule.

## Decision so far
- Main channel: **Web Push + in-app chat (`/chat`)**. Free, works with the app closed.
  Android/Chrome works directly. iPhone only works after "Add to Home Screen".
- Optional extra: WhatsApp via Meta's free test number (personal use only,
  up to 5 verified numbers; free-form messages only within 24h of my last message
  to the bot, templates after that may cost money).
- Telegram: leave out (code is uncommitted in the working tree; harmless while
  `RAGRA_TELEGRAM_BOT_TOKEN` is blank).

## Already built (in the repo)
- Committed (74a15ed): WhatsApp Cloud API channel + signed webhook, chat assistant
  (`ragra/chat.py`, Gemini for free questions), announcement and material alerts.
- Since committed: `/chat` page (`ragra/web/templates/chat.html`, routes in
  `ragra/web/app.py`), chat messages tables (migrations 0031-0033), nags
  (`ragra/reminders/nags.py`), Telegram channel (`ragra/web/telegram.py`, migration 0030).
- The `/chat` page only updates while it is open (it polls `/chat/messages`).

## Next steps (start here)
1. Move hosting to Oracle (follow `docs/DEPLOY-ORACLE.md`), then switch Railway off after the checks pass.
2. Test the feedback email on the live site (both inboxes + `/contact/inbox` says "Sent to 2 of 2").
3. Optional polish: real roles/taglines for the two team cards, a "Send test email" button for Brevo.
4. Ask: Android or iPhone? (decides the install step for push)
5. Build Web Push (the chat work from step 2 of the old list is already committed): PWA manifest, service worker, VAPID keys (private key in Railway
   variables), `push_subscriptions` table, a `WebPushProvider` using the existing
   notification provider interface, delete subscriptions on 404/410, a button on
   `/account` to enable notifications. Each new GCR item should create a chat message
   and a push notification, and tapping it opens `/chat`.
   (Do this after the Oracle move: the service worker needs the stable HTTPS address.)
6. Deploy and test on my real phone with the app closed.
7. Optional: switch on WhatsApp with the `RAGRA_WHATSAPP_*` variables.

## Open questions
- Android or iPhone?
- Keep or delete the Telegram code?
- Should quiz questions be answered by the Gemini assistant or only fixed commands?
