"""Atomic JSONL export of submitted RCR annotations."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

_snapshot_lock = threading.Lock()


def backup_database(db_path: Path, destination: Path) -> Path:
    """Consistent full backup, including committed WAL data while the server runs."""
    db_path = db_path.resolve(strict=True)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids accidentally overwriting an earlier backup.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("backup integrity check failed")
        os.chmod(destination, 0o600)
        with destination.open("rb") as backup:
            os.fsync(backup.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def snapshot_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "-final.jsonl")


def write_snapshot(db_path: Path, destination: Path | None = None) -> Path:
    """Atomically refresh the canonical JSONL of currently submitted tasks."""
    destination = destination or snapshot_path(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _snapshot_lock:
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=".rcr-final-", dir=destination.parent
            )
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                with closing(sqlite3.connect(db_path)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN")
                    for row in db.execute("""
                        SELECT t.sample_id,t.query_image_id,t.target_image_ids_json,
                               a.data_json FROM tasks t JOIN annotations a
                               ON a.task_id=t.sample_id
                        WHERE t.status='SUBMITTED' AND a.submitted=1
                        ORDER BY t.sample_id
                    """):
                        annotation = json.loads(row["data_json"])
                        result = {
                            "sample_id": row["sample_id"],
                            "case_type": annotation["case_type"],
                            "query_image_id": row["query_image_id"],
                            "target_image_ids": json.loads(
                                row["target_image_ids_json"]
                            ),
                            "subjects": annotation["subjects"],
                            "final_desc": annotation["final_desc"],
                            "final_change": annotation["final_change"],
                            "final_instruction": annotation["final_instruction"],
                        }
                        output.write(json.dumps(result, ensure_ascii=False) + "\n")
                    db.rollback()
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            temporary = None
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                os.unlink(temporary)
    return destination
