# My ideas and where I left off

Read this first when resuming work. Update it at the end of every session.
Last updated: 2026-11-06

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
- Already deployed on Railway (HTTPS URL exists).
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
- Uncommitted: `/chat` page (`ragra/web/templates/chat.html`, routes in
  `ragra/web/app.py`), chat messages tables (migrations 0031-0033), nags
  (`ragra/reminders/nags.py`), Telegram channel (`ragra/web/telegram.py`, migration 0030).
- The `/chat` page only updates while it is open (it polls `/chat/messages`).

## Next steps (start here)
1. Ask: Android or iPhone? (decides the install step)
2. Run the tests, then commit the uncommitted chat/nags work (my git identity only,
   no Claude co-author line).
3. Build Web Push: PWA manifest, service worker, VAPID keys (private key in Railway
   variables), `push_subscriptions` table, a `WebPushProvider` using the existing
   notification provider interface, delete subscriptions on 404/410, a button on
   `/account` to enable notifications. Each new GCR item should create a chat message
   and a push notification, and tapping it opens `/chat`.
4. Deploy to Railway and test on my real phone with the app closed.
5. Optional: switch on WhatsApp with the `RAGRA_WHATSAPP_*` variables.

## Open questions
- Android or iPhone?
- Keep or delete the Telegram code?
- Should quiz questions be answered by the Gemini assistant or only fixed commands?
