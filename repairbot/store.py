from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, status TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'triage', data TEXT NOT NULL DEFAULT '{}',
            updated REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS providers (name TEXT PRIMARY KEY, blocked_until REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(providers)")}
        if "source" not in columns:
            self.db.execute("ALTER TABLE providers ADD COLUMN source TEXT NOT NULL DEFAULT 'session'")
        self.db.commit()

    def enqueue(self, job_id: str, content: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,content,status,updated) VALUES(?,?,'queued',?)",
            (job_id, content, time.time()),
        )
        self.db.commit()
        return bool(cur.rowcount)

    def recover(self) -> None:
        self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        self.db.commit()

    def next_job(self) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','waiting') AND retry_at<=? ORDER BY updated LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row is None:
            return None
        self.save(row["id"], status="running")
        job = dict(row)
        job["data"] = json.loads(job["data"])
        return job

    def save(self, job_id: str, **values) -> None:
        if not values.keys() <= {"status", "phase", "data", "retry_at"}:
            raise ValueError("Invalid job fields")
        if "data" in values:
            values["data"] = json.dumps(values["data"], ensure_ascii=False)
        values["updated"] = time.time()
        self.db.execute(
            f"UPDATE jobs SET {', '.join(k + '=?' for k in values)} WHERE id=?",
            (*values.values(), job_id),
        )
        self.db.commit()

    def block(self, name: str, until: float, source: str = "session") -> None:
        self.db.execute("INSERT OR REPLACE INTO providers(name,blocked_until,source) VALUES(?,?,?)", (name, until, source))
        self.db.commit()

    def unblock_query(self, name: str) -> None:
        # A quota query may lift only its own blocks; a limit the CLI reported holds until it expires.
        self.db.execute("DELETE FROM providers WHERE name=? AND source='query'", (name,))
        self.db.commit()

    def blocked_until(self, name: str) -> float:
        row = self.db.execute("SELECT blocked_until FROM providers WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0

    def retry_failed(self, job_id: str) -> bool:
        row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return False
        data = json.loads(row[0])
        # A manual retry starts a fresh CI wait window and re-announces progress.
        data.pop("ci_wait_since", None)
        data.pop("ci_notified", None)
        cur = self.db.execute(
            "UPDATE jobs SET status='queued',retry_at=0,updated=?,data=? WHERE id=? AND status IN ('failed','waiting')",
            (time.time(), json.dumps(data, ensure_ascii=False), job_id),
        )
        self.db.commit()
        return bool(cur.rowcount)

    def cursor(self, channel_id: int) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (f"channel:{channel_id}",)).fetchone()
        return row[0] if row else None

    def bind(self, repository: str, guild_id: int, listen_id: int, report_id: int) -> None:
        identity = json.dumps([repository, guild_id, listen_id, report_id])
        row = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
        if row and row[0] != identity:
            raise ValueError("state_dir 已绑定其他仓库/服务器/频道，请使用新的 state_dir")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (identity,))
        self.db.commit()

    def set_cursor(self, channel_id: int, message_id: int) -> None:
        previous = self.cursor(channel_id)
        if previous is None or message_id > int(previous):
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (f"channel:{channel_id}", str(message_id)))
            self.db.commit()
