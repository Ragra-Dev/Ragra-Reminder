# Google Classroom Rules

Google Classroom is authoritative for academic source data.

Use stable external identifiers (course ID + coursework/announcement/material ID) rather than titles for deduplication.

Inspect real Classroom data before implementing section parsing. If enrollment/course metadata already separates sections, do not add NLP parsing unnecessarily.

Treat Classroom deadline changes as authoritative updates:
1. record the old value
2. update the actual deadline
3. recalculate reminders
4. update the owned calendar event
5. notify Hashim when appropriate

Never invent a deadline when Classroom does not provide one.
