"""SQLite journal and durable exclusion. Locks never expire into permission to replay."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .util import now, uid, dumps, digest

STATES = {"queued", "routing", "routed", "running", "collecting", "completed", "failed", "needs_user", "outcome_unknown", "cancelled"}
TRANSITIONS = {
    "queued": {"routing", "cancelled"}, "routing": {"routed", "needs_user", "outcome_unknown"},
    "routed": {"routing", "running", "needs_user", "cancelled"},
    "running": {"collecting", "failed", "outcome_unknown"},
    "collecting": {"completed", "failed", "outcome_unknown"},
    "completed": {"routing", "running"}, "failed": {"routing", "running", "cancelled"},
    "needs_user": {"routing", "cancelled"}, "outcome_unknown": {"completed", "failed", "cancelled"},
    "cancelled": set()}


class Busy(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.root / "state.sqlite3"), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=10000;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT OR IGNORE INTO meta VALUES('schema_version','1');
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, workspace TEXT NOT NULL, grants TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),goal TEXT NOT NULL,intent TEXT NOT NULL,provider TEXT,status TEXT NOT NULL,blockers TEXT NOT NULL DEFAULT '',verified INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,task_id TEXT REFERENCES tasks(id),project_id TEXT REFERENCES projects(id),created TEXT NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS records_kind ON records(kind,seq);
        CREATE TABLE IF NOT EXISTS locks(resource TEXT PRIMARY KEY,owner TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cursors(project_id TEXT PRIMARY KEY REFERENCES projects(id),seq INTEGER NOT NULL);
        """)
        if self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "1":
            raise ValueError("unsupported database schema")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def add_project(self, name, workspace, grants):
        from .policy import ACTIONS
        workspace = Path(workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("project workspace must exist")
        if type(grants) is not list or any(type(g) is not str or g not in ACTIONS for g in grants):
            raise ValueError("malformed project grants")
        ident = uid("p")
        self.db.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (ident, name, str(workspace), dumps(grants), now()))
        return self.project(ident)

    def project(self, name):
        row = self.db.execute("SELECT * FROM projects WHERE id=? OR name=?", (name, name)).fetchone()
        if not row:
            raise ValueError("unknown project: " + name)
        result = dict(row)
        result["grants"] = json.loads(result["grants"])
        return result

    def projects(self):
        return [self.project(r[0]) for r in self.db.execute("SELECT id FROM projects ORDER BY created,id")]

    def add_task(self, project, goal, intent="read", provider=None, blockers=""):
        if intent not in {"read", "write_workspace"} or provider not in {None, "codex", "claude_code"}:
            raise ValueError("invalid task intent/provider")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal cannot be empty")
        p = self.project(project)
        ident, stamp = uid("t"), now()
        self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ident, p["id"], goal, intent, provider, "queued", blockers, 0, stamp, stamp))
        return self.task(ident)

    def task(self, ident):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("unknown task: " + ident)
        return dict(row)

    def tasks(self, project=None):
        if project:
            rows = self.db.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY created DESC,id", (self.project(project)["id"],))
        else:
            rows = self.db.execute("SELECT id FROM tasks ORDER BY created DESC,id")
        return [self.task(r[0]) for r in rows]

    def transition(self, ident, status, blockers=None):
        old = self.task(ident)
        if status not in STATES or status not in TRANSITIONS[old["status"]]:
            raise ValueError("invalid task transition: %s -> %s" % (old["status"], status))
        changed = self.db.execute("UPDATE tasks SET status=?,blockers=?,updated=?,verified=0 WHERE id=? AND status=? AND updated=?",
                        (status, old["blockers"] if blockers is None else blockers, now(), ident, old["status"], old["updated"]))
        if changed.rowcount != 1:
            raise Busy("task state changed concurrently")
        self.record("event", {"action": "transition", "from": old["status"], "to": status}, ident, old["project_id"])

    def record(self, kind, data, task_id=None, project_id=None, ident=None):
        ident = ident or uid(kind)
        self.db.execute("INSERT INTO records(id,kind,task_id,project_id,created,data) VALUES(?,?,?,?,?,?)",
                        (ident, kind, task_id, project_id, now(), dumps(data)))
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("unknown record: " + ident)
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def records(self, kind=None, task_id=None, project_id=None, cutoff=None):
        clauses, args = [], []
        for key, value in (("kind", kind), ("task_id", task_id), ("project_id", project_id)):
            if value is not None:
                clauses.append(key + "=?")
                args.append(value)
        if cutoff is not None:
            clauses.append("seq<=?")
            args.append(cutoff)
        sql = "SELECT id FROM records" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY seq DESC"
        return [self.get(r[0]) for r in self.db.execute(sql, args)]

    def update_record(self, ident, data, owner=None):
        # Run completion is fenced by the durable token, even after explicit reconciliation.
        if owner and not self.db.execute("SELECT 1 FROM locks WHERE owner=?", (owner,)).fetchone():
            raise Busy("stale run owner; completion refused")
        self.db.execute("UPDATE records SET data=? WHERE id=?", (dumps(data), ident))

    def _os_lock(self, resource):
        import fcntl
        path = self.root / "locks" / (digest(resource) + ".lock")
        path.parent.mkdir(exist_ok=True)
        f = open(path, "a+b")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise Busy("resource has a live process: " + resource)
        return f

    @contextmanager
    def claim(self, resources):
        resources = sorted(set(resources))
        handles, owner = [], uid("owner")
        try:
            for resource in resources:
                handles.append(self._os_lock(resource))
            with self.transaction():
                for resource in resources:
                    if self.db.execute("SELECT 1 FROM locks WHERE resource=?", (resource,)).fetchone():
                        raise Busy("unresolved operation; inspect/reconcile before retry: " + resource)
                for resource in resources:
                    self.db.execute("INSERT INTO locks VALUES(?,?,?)", (resource, owner, now()))
            try:
                yield owner, [h.fileno() for h in handles]
            finally:
                self.db.execute("DELETE FROM locks WHERE owner=?", (owner,))
        finally:
            for handle in handles:
                handle.close()

    @contextmanager
    def inspection_lock(self, resources):
        """Inspect stale journal rows while excluding any surviving process."""
        handles = []
        try:
            for resource in sorted(set(resources)):
                handles.append(self._os_lock(resource))
            yield
        finally:
            for handle in handles:
                handle.close()

    def reconcile_run(self, ident, resolution=None, reason=None):
        run = self.get(ident)
        if run["kind"] != "run":
            raise ValueError("expected run ID")
        data = run["data"]
        if data["status"] not in {"running", "outcome_unknown"}:
            return run
        resources = data["resources"]
        handles = []
        try:
            for resource in resources:
                handles.append(self._os_lock(resource))
            if resolution and (resolution not in {"completed", "failed", "cancelled"} or not reason or not reason.strip()):
                raise ValueError("resolution requires completed/failed/cancelled and a reason")
            with self.transaction():
                task = self.task(run["task_id"])
                if not data.get("review") and task["status"] in {"running", "collecting"}:
                    self.transition(task["id"], "outcome_unknown", "interrupted run; outcome unknown")
                data["status"] = "outcome_unknown"
                if resolution:
                    data.update(status=resolution, resolution_reason=reason, operator_resolved=True)
                    if not data.get("review"):
                        self.transition(task["id"], resolution, reason)
                    self.db.execute("DELETE FROM locks WHERE owner=?", (data["owner"],))
                self.update_record(ident, data)
                self.record("event", {"action": "reconcile", "run": ident, "resolution": resolution, "reason": reason}, task["id"], task["project_id"])
        finally:
            for handle in handles:
                handle.close()
        return self.get(ident)

    def reconcile_routing(self, task_id):
        task = self.task(task_id)
        if task["status"] != "routing":
            raise ValueError("task-level reconcile is for interrupted routing; use run/publication ID otherwise")
        resource = "task:" + task_id
        handle = self._os_lock(resource)
        try:
            with self.transaction():
                self.transition(task_id, "needs_user", "interrupted classification; explicit route required")
                self.db.execute("DELETE FROM locks WHERE resource=?", (resource,))
                self.record("event", {"action": "reconcile_routing", "inspection": "no live classifier lock"}, task_id, task["project_id"])
        finally:
            handle.close()
        return self.task(task_id)
