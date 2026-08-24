# Security Rules

- Never read secrets for the purpose of displaying them.
- Never put credentials, tokens, cookies, session files, or private keys into source code, tests, logs, screenshots, README files, or commits.
- Check `.gitignore` before creating credential/token files.
- Keep OAuth and Hermes authentication server/local-side; never expose secrets to browser code.
- Treat Google Classroom data, timetable data, messages, and academic history as private user data.
- Prefer least-privilege OAuth scopes.
