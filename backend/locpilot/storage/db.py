"""Application database (separate from LangGraph's checkpoint database)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('github', 'local')),
  source_url TEXT,
  path TEXT NOT NULL UNIQUE,
  managed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('ask', 'code')),
  status TEXT NOT NULL,
  phase TEXT,
  turn INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  workspace_path TEXT,
  worktree_path TEXT,
  base_commit TEXT,
  model TEXT,
  error TEXT,
  stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_repo ON tasks(repo_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  turn INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  mode TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  turn INTEGER NOT NULL,
  type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  turn INTEGER NOT NULL,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, turn)
);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  turn INTEGER NOT NULL,
  stage TEXT NOT NULL,
  argv_json TEXT NOT NULL,
  cwd TEXT NOT NULL,
  status TEXT NOT NULL,
  exit_code INTEGER,
  stdout TEXT,
  stderr TEXT,
  duration_ms INTEGER,
  timed_out INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  sandbox TEXT NOT NULL DEFAULT 'none',
  summary_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_task ON commands(task_id, started_at);
CREATE TABLE IF NOT EXISTS patches (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  turn INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  diff TEXT NOT NULL,
  changes_json TEXT NOT NULL,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_patches_task ON patches(task_id, created_at);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  patch_id TEXT,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id, created_at);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "needs_input"}
ACTIVE_STATUSES = {"queued", "running"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # settings -----------------------------------------------------------------------
    def get_setting(self, key: str) -> Any:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return _loads(row["value"], None) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # repositories -------------------------------------------------------------------
    def add_repo(self, *, name: str, source_type: str, source_url: str | None, path: str, managed: bool) -> dict:
        existing = self._one("SELECT * FROM repositories WHERE path=?", (path,))
        ts = now_iso()
        if existing:
            self._exec("UPDATE repositories SET last_opened_at=? WHERE id=?", (ts, existing["id"]))
            return self.get_repo(existing["id"])  # type: ignore[return-value]
        repo_id = new_id("r")
        self._exec(
            "INSERT INTO repositories(id, name, source_type, source_url, path, managed, created_at, last_opened_at)"
            " VALUES(?,?,?,?,?,?,?,?)", (repo_id, name, source_type, source_url, path, int(managed), ts, ts))
        return self.get_repo(repo_id)  # type: ignore[return-value]

    def get_repo(self, repo_id: str) -> dict | None:
        row = self._one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        if row:
            row["managed"] = bool(row["managed"])
        return row

    def find_repo_by_url(self, url: str) -> dict | None:
        row = self._one("SELECT * FROM repositories WHERE source_url=?", (url,))
        if row:
            row["managed"] = bool(row["managed"])
        return row

    def list_repos(self) -> list[dict]:
        rows = self._all("SELECT * FROM repositories ORDER BY last_opened_at DESC")
        for row in rows:
            row["managed"] = bool(row["managed"])
        return rows

    def touch_repo(self, repo_id: str) -> None:
        self._exec("UPDATE repositories SET last_opened_at=? WHERE id=?", (now_iso(), repo_id))

    def delete_repo(self, repo_id: str) -> None:
        self._exec("DELETE FROM repositories WHERE id=?", (repo_id,))

    # tasks ----------------------------------------------------------------------------
    def create_task(self, *, repo_id: str, title: str, mode: str, model: str | None) -> dict:
        task_id = new_id("t")
        ts = now_iso()
        self._exec("INSERT INTO tasks(id, repo_id, title, mode, status, phase, turn, created_at, updated_at, model)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)", (task_id, repo_id, title, mode, "queued", None, 0, ts, ts, model))
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict | None:
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["stats"] = _loads(row.pop("stats_json"), {})
        return row

    def list_tasks(self, repo_id: str | None = None, limit: int = 200) -> list[dict]:
        if repo_id:
            rows = self._all("SELECT * FROM tasks WHERE repo_id=? ORDER BY updated_at DESC LIMIT ?", (repo_id, limit))
        else:
            rows = self._all("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
        for row in rows:
            row["stats"] = _loads(row.pop("stats_json"), {})
        return rows

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "stats" in fields:
            fields["stats_json"] = json.dumps(fields.pop("stats"))
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE tasks SET {cols} WHERE id=?", [*fields.values(), task_id])

    def delete_task(self, task_id: str) -> None:
        self._exec("DELETE FROM tasks WHERE id=?", (task_id,))

    def mark_interrupted_tasks(self) -> list[str]:
        rows = self._all("SELECT id FROM tasks WHERE status IN ('queued', 'running')")
        ids = [r["id"] for r in rows]
        for task_id in ids:
            self.update_task(task_id, status="interrupted")
        self._exec("UPDATE commands SET status='interrupted', finished_at=? WHERE status='running'", (now_iso(),))
        return ids

    # messages -------------------------------------------------------------------------
    def add_message(self, task_id: str, *, turn: int, role: str, content: str, mode: str | None) -> dict:
        cur = self._exec("INSERT INTO messages(task_id, turn, role, content, mode, created_at) VALUES(?,?,?,?,?,?)",
                         (task_id, turn, role, content, mode, now_iso()))
        return self._one("SELECT * FROM messages WHERE id=?", (cur.lastrowid,))  # type: ignore[return-value]

    def list_messages(self, task_id: str) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE task_id=? ORDER BY id", (task_id,))

    # events ---------------------------------------------------------------------------
    def add_event(self, task_id: str, *, turn: int, type: str, data: dict) -> dict:
        ts = now_iso()
        cur = self._exec("INSERT INTO events(task_id, turn, type, data_json, created_at) VALUES(?,?,?,?,?)",
                         (task_id, turn, type, json.dumps(data, default=str), ts))
        return {"id": cur.lastrowid, "task_id": task_id, "turn": turn, "type": type, "data": data, "created_at": ts}

    def list_events(self, task_id: str, after: int = 0, limit: int = 5000) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?", (task_id, after, limit))
        return [{"id": r["id"], "task_id": r["task_id"], "turn": r["turn"], "type": r["type"],
                 "data": _loads(r["data_json"], {}), "created_at": r["created_at"]} for r in rows]

    # reports --------------------------------------------------------------------------
    def save_report(self, task_id: str, turn: int, report: dict) -> None:
        self._exec("INSERT INTO reports(task_id, turn, report_json, created_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(task_id, turn) DO UPDATE SET report_json=excluded.report_json",
                   (task_id, turn, json.dumps(report, default=str), now_iso()))

    def list_reports(self, task_id: str) -> list[dict]:
        rows = self._all("SELECT report_json FROM reports WHERE task_id=? ORDER BY turn", (task_id,))
        return [_loads(r["report_json"], {}) for r in rows]

    # commands -------------------------------------------------------------------------
    def start_command(self, task_id: str, *, turn: int, stage: str, argv: list[str], cwd: str, sandbox: str) -> dict:
        cmd_id = new_id("c")
        self._exec("INSERT INTO commands(id, task_id, turn, stage, argv_json, cwd, status, sandbox, started_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (cmd_id, task_id, turn, stage, json.dumps(argv), cwd, "running", sandbox, now_iso()))
        return self.get_command(cmd_id)  # type: ignore[return-value]

    def finish_command(self, cmd_id: str, *, status: str, exit_code: int | None, stdout: str, stderr: str,
                       duration_ms: int, timed_out: bool, truncated: bool, summary: dict | None) -> dict:
        self._exec("UPDATE commands SET status=?, exit_code=?, stdout=?, stderr=?, duration_ms=?, timed_out=?,"
                   " truncated=?, summary_json=?, finished_at=? WHERE id=?",
                   (status, exit_code, stdout, stderr, duration_ms, int(timed_out), int(truncated),
                    json.dumps(summary) if summary is not None else None, now_iso(), cmd_id))
        return self.get_command(cmd_id)  # type: ignore[return-value]

    @staticmethod
    def _command_row(row: dict, include_output: bool) -> dict:
        out = {
            "id": row["id"], "turn": row["turn"], "stage": row["stage"], "argv": _loads(row["argv_json"], []),
            "cwd": row["cwd"], "status": row["status"], "exit_code": row["exit_code"],
            "duration_ms": row["duration_ms"], "timed_out": bool(row["timed_out"]),
            "truncated": bool(row["truncated"]), "sandbox": row["sandbox"],
            "summary": _loads(row["summary_json"], None), "started_at": row["started_at"],
            "finished_at": row["finished_at"], "stdout": None, "stderr": None,
        }
        if include_output:
            out["stdout"], out["stderr"] = row["stdout"], row["stderr"]
        return out

    def get_command(self, cmd_id: str, include_output: bool = True) -> dict | None:
        row = self._one("SELECT * FROM commands WHERE id=?", (cmd_id,))
        return self._command_row(row, include_output) if row else None

    def list_commands(self, task_id: str, include_output: bool = False) -> list[dict]:
        rows = self._all("SELECT * FROM commands WHERE task_id=? ORDER BY started_at, rowid", (task_id,))
        return [self._command_row(r, include_output) for r in rows]

    # patches --------------------------------------------------------------------------
    def add_patch(self, task_id: str, *, turn: int, attempt: int, summary: str, diff: str, changes: list[dict],
                  warnings: list[str]) -> dict:
        patch_id = new_id("p")
        self._exec("INSERT INTO patches(id, task_id, turn, attempt, status, summary, diff, changes_json, warnings_json,"
                   " created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (patch_id, task_id, turn, attempt, "proposed", summary, diff, json.dumps(changes),
                    json.dumps(warnings), now_iso()))
        return self.get_patch(patch_id)  # type: ignore[return-value]

    def get_patch(self, patch_id: str) -> dict | None:
        row = self._one("SELECT * FROM patches WHERE id=?", (patch_id,))
        return self._patch_row(row) if row else None

    @staticmethod
    def _patch_row(row: dict) -> dict:
        changes = _loads(row["changes_json"], [])
        return {
            "id": row["id"], "turn": row["turn"], "attempt": row["attempt"], "status": row["status"],
            "summary": row["summary"], "diff": row["diff"], "changes": changes,
            "files": [{k: c[k] for k in ("path", "change", "kind", "additions", "deletions")} for c in changes],
            "warnings": _loads(row["warnings_json"], []), "created_at": row["created_at"],
            "decided_at": row["decided_at"], "applied_at": row["applied_at"],
        }

    def list_patches(self, task_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM patches WHERE task_id=? ORDER BY created_at, rowid", (task_id,))
        return [self._patch_row(r) for r in rows]

    def update_patch(self, patch_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE patches SET {cols} WHERE id=?", [*fields.values(), patch_id])

    # approvals ------------------------------------------------------------------------
    def add_approval(self, task_id: str, *, kind: str, patch_id: str | None, payload: dict) -> dict:
        approval_id = new_id("a")
        self._exec("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending'", (task_id,))
        self._exec("INSERT INTO approvals(id, task_id, kind, patch_id, status, payload_json, created_at)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (approval_id, task_id, kind, patch_id, "pending", json.dumps(payload), now_iso()))
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return self._approval_row(row) if row else None

    @staticmethod
    def _approval_row(row: dict) -> dict:
        return {"id": row["id"], "task_id": row["task_id"], "kind": row["kind"], "patch_id": row["patch_id"],
                "status": row["status"], "payload": _loads(row["payload_json"], {}),
                "decision": _loads(row["decision_json"], None), "created_at": row["created_at"],
                "decided_at": row["decided_at"]}

    def pending_approval(self, task_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE task_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                        (task_id,))
        return self._approval_row(row) if row else None

    def decide_approval(self, approval_id: str, *, status: str, decision: dict) -> None:
        self._exec("UPDATE approvals SET status=?, decision_json=?, decided_at=? WHERE id=?",
                   (status, json.dumps(decision), now_iso(), approval_id))
