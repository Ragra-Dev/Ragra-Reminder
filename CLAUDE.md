# Ragra Personal Edition

Ragra is a single-user academic operating system for Hashim. It integrates Google Classroom, Google Calendar, FAST timetable data, and the existing Hermes messaging bridge.

## Operating rules
- Inspect existing code and Hermes capabilities before changing or rebuilding anything.
- Prefer the smallest reliable implementation. Do not build SaaS/multi-user infrastructure yet.
- Build vertically: implement one end-to-end slice, test it, then continue.
- Never invent academic facts. Google Classroom is authoritative for academic deadlines.
- Keep actual academic deadlines separate from Hashim's personal completion targets.
- Synchronization and reminder jobs must be idempotent: reruns must not duplicate tasks, events, or notifications.
- Never expose, print, commit, or transmit secrets, OAuth tokens, refresh tokens, or Hermes session data.
- Reuse Hermes notification integrations rather than rebuilding WhatsApp/Telegram/iOS messaging.
- Use deterministic code for hard constraints; AI may propose plans but cannot overwrite authoritative facts.
- Run relevant tests after meaningful changes and fix regressions before moving on.
- Do not add dependencies, abstractions, or architecture unless they solve a current problem.
- If a requirement is ambiguous, inspect the real data/code first; do not guess.
- If a major architectural decision is required, explain the tradeoff briefly before implementing it.

## Source of truth
Read `docs/PRODUCT.md` for product behavior and priorities.
Read `docs/DOMAIN.md` for deadline/task semantics.
Read `docs/ARCHITECTURE.md` when changing system boundaries or integrations.

## Local overrides
If local Ragra preferences are needed, use the imported local configuration described in `docs/ragra-local-import.example.md` rather than creating `CLAUDE.local.md`.
