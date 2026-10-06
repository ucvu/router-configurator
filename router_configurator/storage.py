from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .lists import RoutingList, router_key


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    router_key TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action = 'update'),
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
                    snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
            """)
            # Upgrade the database created by the sequential-worker version in place.
            db.execute("BEGIN IMMEDIATE")
            columns = {column["name"] for column in db.execute("PRAGMA table_info(jobs)")}
            if "router_key" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN router_key TEXT NOT NULL DEFAULT ''")
            for row in db.execute("SELECT id,hostname FROM jobs WHERE router_key=''").fetchall():
                db.execute("UPDATE jobs SET router_key=? WHERE id=?", (router_key(row["hostname"]), row["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS jobs_router_status ON jobs(router_key, status)")

    def enqueue(self, hostname: str, routing: RoutingList) -> str:
        job_id = str(uuid.uuid4())
        created = utc_now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO jobs(id,hostname,router_key,action,status,snapshot,created_at,message) VALUES(?,?,?,'update','queued',?,?,?)",
                (job_id, hostname, router_key(hostname), routing.serialize(), created, "Задача ожидает выполнения."),
            )
            db.execute("INSERT INTO events(job_id,created_at,message) VALUES(?,?,?)", (job_id, created, "Задача принята."))
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT id AS job_id,hostname,action,status,created_at,started_at,finished_at,message,error FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["events"] = [dict(event) for event in db.execute(
                "SELECT created_at,message FROM events WHERE job_id=? ORDER BY id", (job_id,),
            )]
        return result

    def claim_next(self) -> dict | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT queued.* FROM jobs AS queued
                WHERE queued.status='queued' AND NOT EXISTS (
                    SELECT 1 FROM jobs AS active
                    WHERE active.status='running' AND active.router_key=queued.router_key
                )
                ORDER BY queued.created_at, queued.rowid LIMIT 1
            """).fetchone()
            if row is None:
                return None
            started = utc_now()
            db.execute("UPDATE jobs SET status='running',started_at=?,message=? WHERE id=?",
                       (started, "Задача выполняется.", row["id"]))
            db.execute("INSERT INTO events(job_id,created_at,message) VALUES(?,?,?)", (row["id"], started, "Задача запущена."))
            return dict(row)

    def event(self, job_id: str, message: str) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO events(job_id,created_at,message) VALUES(?,?,?)", (job_id, utc_now(), message))

    def finish(self, job_id: str, error: str | None = None) -> None:
        message = error if error is not None else "Списки и DNS-маршруты обновлены."
        finished = utc_now()
        with self.connection() as db:
            db.execute("UPDATE jobs SET status=?,finished_at=?,message=?,error=? WHERE id=?",
                       ("failed" if error is not None else "succeeded", finished, message, error, job_id))
            db.execute("INSERT INTO events(job_id,created_at,message) VALUES(?,?,?)", (job_id, finished, message))

    def interrupt_running(self) -> None:
        message = "Выполнение прервано остановкой службы; автоматический повтор не выполняется."
        finished = utc_now()
        with self.connection() as db:
            db.execute("INSERT INTO events(job_id,created_at,message) SELECT id,?,? FROM jobs WHERE status='running'",
                       (finished, message))
            db.execute("UPDATE jobs SET status='failed',finished_at=?,message=?,error=? WHERE status='running'",
                       (finished, message, message))
