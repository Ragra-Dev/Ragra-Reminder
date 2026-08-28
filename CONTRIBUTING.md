# Contributing to Ragra

## Setup

### Prerequisites
- Python 3.11 or later
- Git

### Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd "<REPOSITORY_ROOT>"
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. **Install the project with development dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Verify the installation:**
   ```bash
   python -m pytest
   ```

All tests should pass (211 passing as of the latest main branch).

## Running Ragra

Once installed, you can run:

```bash
# Serve the dashboard locally (http://127.0.0.1:8731/)
python -m ragra.cli serve

# Run a complete sync + reminder dispatch cycle
python -m ragra.cli sync

# Run a full tick (sync + reminders, logs to file)
python -m ragra.cli tick

# Print the daily brief
python -m ragra.cli brief
```

## Configuration

Before running sync commands, copy `.env.example` to `.env` and fill in the required local paths:

```bash
cp .env.example .env
# Edit .env with your local paths
```

**Important:** Never commit `.env` or any credentials. The `.gitignore` protects `.env*` files and OAuth token/credential JSON files. All secrets stay local.

## Development Rules

- Read `ROADMAP.md` at the start of a session to understand the current phase and where a change fits in it.
- Run the full test suite after meaningful changes: `python -m pytest`.
- Inspect the codebase before changing architecture — read `docs/ARCHITECTURE.md` for context.
- **Git commits:** Use concise, professional messages that describe the logical engineering change. The code, tests, documentation, and PR descriptions carry the technical detail — commit messages should not. Never include personal information, credentials, internal debugging history, or excessive implementation details.

## Database Backups

Before syncing real data, back up the SQLite database. A backup script is available:

```bash
python -m ragra.scripts.backup
```

This creates a timestamped backup copy in `backups/`.

## Testing

Run all tests:
```bash
python -m pytest
```

Run a specific test file:
```bash
python -m pytest tests/test_reminders_engine.py
```

Run tests matching a pattern:
```bash
python -m pytest -k "classroom"
```

## Questions?

Refer to `ROADMAP.md` for phase-level decisions, `docs/PROJECT_STATUS.md` for current status, and `docs/ARCHITECTURE.md` for system design.
