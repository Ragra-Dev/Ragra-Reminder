"""Minimal SQLite backup script."""
import shutil
from pathlib import Path
from datetime import datetime

from ragra.config import load_config


def main() -> None:
    config = load_config()
    db_path = Path(config.db_path)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.{timestamp}.backup"

    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")


if __name__ == "__main__":
    main()
