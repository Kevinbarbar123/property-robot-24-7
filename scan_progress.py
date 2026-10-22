from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from reliability_config import recovery_path
from source_errors import SourceAccessError


CURRENT_JOB = ""
CURRENT_STORE = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, options TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, position TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS work (job_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,key));
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, job_id TEXT, kind TEXT, provider TEXT, url TEXT, retry_at REAL, status TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, content TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_start REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, host TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, status TEXT NOT NULL, message_id TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS links (hash TEXT PRIMARY KEY, incident_id TEXT, expires REAL, claimed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS portal_sessions (hash TEXT PRIMARY KEY, incident_id TEXT, expires REAL, last_active REAL);
PRAGMA user_version=1;
"""


class ProgressStore:
    def __init__(self, path: Path | None = None):
        self.path = path or recovery_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key, default=None):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def maintain(self):
        now = time.time()
        with self.connect() as connection:
            connection.execute("DELETE FROM cache WHERE expires<?", (now,))
            connection.execute("DELETE FROM links WHERE expires<?", (now,))
            connection.execute("DELETE FROM portal_sessions WHERE expires<?", (now,))
            connection.execute("DELETE FROM work WHERE job_id IN (SELECT id FROM jobs WHERE status='complete' AND updated<?)", (now - 30 * 86400,))

    def set(self, key, value):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def begin(self, options):
        encoded = json.dumps(options, sort_keys=True)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE options=? AND status!='complete' ORDER BY created DESC LIMIT 1", (encoded,)).fetchone()
            if row:
                return row["id"]
            job_id = secrets.token_hex(12)
            connection.execute("INSERT INTO jobs(id,options,status,created,updated) VALUES (?,?,?,?,?)", (job_id, encoded, "running", time.time(), time.time()))
            return job_id

    def job(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else {}

    def latest_job(self):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE status!='complete' ORDER BY created DESC LIMIT 1").fetchone()
        return dict(row) if row else {}

    def position(self, job_id, **position):
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET position=?,updated=? WHERE id=?", (json.dumps(position), time.time(), job_id))

    def status(self, job_id, status):
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (status, time.time(), job_id))
            if status == "complete":
                connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("last_completed_job", json.dumps(job_id)))

    def work(self, job_id, key):
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM work WHERE job_id=? AND key=?", (job_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def save_work(self, job_id, key, payload):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO work VALUES (?,?,?)", (job_id, key, json.dumps(payload)))

    def completed(self, job_id):
        with self.connect() as connection:
            return connection.execute("SELECT count(*) FROM work WHERE job_id=?", (job_id,)).fetchone()[0]

    def completed_listings(self, job_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM work WHERE job_id=? AND key LIKE 'search:%'", (job_id,)).fetchall()
        urls = set()
        for row in rows:
            for item in json.loads(row[0]).get("details", []):
                urls.add(item["listing"]["url"])
        return len(urls)

    def incident(self, error, job_id=""):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM incidents WHERE status='open' LIMIT 1").fetchone()
            if row:
                if row["kind"] not in {"captcha", "schema_changed"} and row["retry_at"] <= time.time():
                    connection.execute("UPDATE incidents SET kind=?,provider=?,url=?,retry_at=?,updated=? WHERE id=?", (error.kind, error.provider, error.url, error.retry_at, time.time(), row["id"]))
                    row = connection.execute("SELECT * FROM incidents WHERE id=?", (row["id"],)).fetchone()
                return dict(row)
            incident_id = secrets.token_hex(12)
            now = time.time()
            connection.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?)", (incident_id, job_id, error.kind, error.provider, error.url, error.retry_at, "open", now, now))
        return self.current_incident()

    def current_incident(self):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM incidents WHERE status='open' ORDER BY created DESC LIMIT 1").fetchone()
        return dict(row) if row else {}

    def resolve(self, incident_id):
        with self.connect() as connection:
            connection.execute("UPDATE incidents SET status='resolved',updated=? WHERE id=?", (time.time(), incident_id))
            connection.execute("DELETE FROM portal_sessions WHERE incident_id=?", (incident_id,))
            connection.execute("DELETE FROM links WHERE incident_id=?", (incident_id,))

    def gate(self, host, config):
        lease_id = secrets.token_hex(12)
        while True:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = time.time()
                incident = connection.execute("SELECT * FROM incidents WHERE status='open' LIMIT 1").fetchone()
                if incident and (incident["kind"] in {"captcha", "schema_changed"} or incident["retry_at"] > now):
                    raise SourceAccessError("OLX session is paused; see /status or /verify.", kind=incident["kind"], url=incident["url"], retry_at=incident["retry_at"], provider=incident["provider"])
                connection.execute("DELETE FROM leases WHERE expires<=?", (now,))
                count = connection.execute("SELECT count(*) FROM leases WHERE host=?", (host,)).fetchone()[0]
                row = connection.execute("SELECT next_start FROM hosts WHERE host=?", (host,)).fetchone()
                wait = max(0, (row[0] if row else 0) - now)
                if count < config.max_in_flight and not wait:
                    import random
                    connection.execute("INSERT OR REPLACE INTO hosts VALUES (?,?)", (host, now + random.uniform(config.request_interval_min, config.request_interval_max)))
                    connection.execute("INSERT INTO leases VALUES (?,?,?)", (lease_id, host, now + 120))
                    return lease_id
            time.sleep(min(max(wait, 0.1), 1))

    def release(self, lease_id):
        with self.connect() as connection:
            connection.execute("DELETE FROM leases WHERE id=?", (lease_id,))

    def cache(self, url):
        with self.connect() as connection:
            row = connection.execute("SELECT content FROM cache WHERE url=? AND expires>?", (url, time.time())).fetchone()
        return row[0] if row else None

    def save_cache(self, url, content, seconds):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (url, content, time.time() + seconds))

    def claim_delivery(self, key):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM deliveries WHERE key=?", (key,)).fetchone()
            if row and row[0] in {"sent", "sending", "unknown"}:
                return False
            connection.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?)", (key, "sending", "", time.time()))
        return True

    def delivered(self, key, status, message_id=""):
        with self.connect() as connection:
            connection.execute("UPDATE deliveries SET status=?,message_id=?,updated=? WHERE key=?", (status, str(message_id), time.time(), key))

    def delivery_status(self, key):
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM deliveries WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def link(self, incident_id, seconds=900):
        token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("DELETE FROM links WHERE incident_id=?", (incident_id,))
            connection.execute("INSERT INTO links(hash,incident_id,expires) VALUES (?,?,?)", (digest(token), incident_id, time.time() + seconds))
        return token

    def redeem(self, token, seconds):
        session = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM links WHERE hash=? AND claimed=0 AND expires>?", (digest(token), time.time())).fetchone()
            if not row:
                raise PermissionError("Verification link expired or already used. Send /verify for a new link.")
            incident = connection.execute("SELECT status FROM incidents WHERE id=?", (row["incident_id"],)).fetchone()
            if not incident or incident[0] != "open":
                raise PermissionError("Verification incident is no longer active")
            connection.execute("UPDATE links SET claimed=1 WHERE hash=?", (digest(token),))
            connection.execute("INSERT INTO portal_sessions VALUES (?,?,?,?)", (digest(session), row["incident_id"], time.time() + seconds, time.time()))
        return session, row["incident_id"]

    def authorize(self, session, idle_seconds, touch=True):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM portal_sessions WHERE hash=? AND expires>? AND last_active>?", (digest(session), time.time(), time.time() - idle_seconds)).fetchone()
            if not row:
                raise PermissionError("Verification session expired")
            if touch:
                connection.execute("UPDATE portal_sessions SET last_active=? WHERE hash=?", (time.time(), digest(session)))
            return row["incident_id"]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def recovery_worker_lock():
    path = recovery_path().with_suffix(".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
