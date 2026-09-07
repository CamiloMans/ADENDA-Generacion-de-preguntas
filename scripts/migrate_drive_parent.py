"""Copy the ICSARA Drive tree to a new parent folder and remap DB references.

Usage (dry-run by default):

    python scripts/migrate_drive_parent.py --source <OLD_FOLDER_ID> --target <NEW_FOLDER_ID>
    python scripts/migrate_drive_parent.py --source ... --target ... --apply
    python scripts/migrate_drive_parent.py --source ... --target ... --verify

The mapping file (default ``scripts/out/drive_migration_<source>_<target>.json``)
is written after every copied item, so an interrupted ``--apply`` run can simply
be re-executed. The source folder is never modified or deleted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db.session import SessionLocal  # noqa: E402
from app.services.drive_migration import run_migration, verify_tree_counts  # noqa: E402
from app.services.google_drive_service import GoogleDriveService  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Current parent folder id (will not be modified).")
    parser.add_argument("--target", required=True, help="New parent folder id.")
    parser.add_argument("--mapping", type=Path, default=None, help="Path of the resumable mapping JSON.")
    parser.add_argument("--apply", action="store_true", help="Actually copy files and update the database.")
    parser.add_argument("--verify", action="store_true", help="Only print recursive folder/file counts for both trees.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    drive = GoogleDriveService.from_settings()

    if args.verify:
        print(json.dumps({"source": verify_tree_counts(drive, args.source), "target": verify_tree_counts(drive, args.target)}, indent=2))
        return 0

    mapping_path = args.mapping or (ROOT_DIR / "scripts" / "out" / f"drive_migration_{args.source}_{args.target}.json")
    db = SessionLocal()
    try:
        report = run_migration(
            drive,
            db,
            source_id=args.source,
            target_id=args.target,
            mapping_path=mapping_path,
            apply=args.apply,
            log=print,
        )
    finally:
        db.close()

    print(json.dumps({"apply": args.apply, "mapping": str(mapping_path), **report.as_dict()}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
