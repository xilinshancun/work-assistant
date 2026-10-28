import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from work_assistant.store import Store, Busy


class CLITests(unittest.TestCase):
    def test_init_project_task_evidence_claim_report_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            conf = root / "config.json"
            def cli(*args, expect=0):
                run = subprocess.run([sys.executable, "-m", "work_assistant", "--config", str(conf)] + list(args),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(run.returncode, expect, run.stderr.decode())
                return json.loads(run.stdout) if expect == 0 else run.stderr.decode()
            cli("init")
            cli("project", "add", "cli-project", "--workspace", str(root))
            task = cli("task", "add", "cli-project", "synthetic read-only goal")
            self.assertEqual(task["intent"], "read")
            self.assertEqual(len(cli("task", "list")), 1)
            self.assertEqual(cli("task", "show", task["id"])["task"]["status"], "queued")
            self.assertIn("route the task", cli("run", task["id"], expect=2))
            source = root / "sample.json"
            source.write_text('{"values":{"count":{"value":7,"unit":"items"}}}')
            evidence = cli("evidence", "add", "cli-project", str(source), "--scope", "synthetic CLI")
            cli("claim", "add", "cli-project", "seven synthetic items", "--evidence", evidence["id"],
                "--numeric", '{"key":"count","value":7,"unit":"items"}')
            report = cli("report", "cli-project", "--date", "2026-01-15")
            self.assertEqual(cli("publish", report["id"], "--dry-run")["status"], "dry_run")
            self.assertIn("not configured", cli("publish", report["id"], "--confirm", expect=2))

    def test_crashed_parent_preserves_running_and_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            s = Store(root / "state")
            self.addCleanup(s.close)
            project = s.add_project("crash", root, ["read", "write_workspace"])
            task = s.add_task(project["id"], "synthetic crash fixture", "write_workspace")
            ident = task["id"]
            # Abrupt exit skips claim's finally. No shell, network or provider involved.
            script = '''import os,sys
from work_assistant.store import Store
s=Store(sys.argv[1])
t=s.task(sys.argv[2]);p=s.project(t['project_id'])
resources=['task:'+t['id'],'workspace:'+p['workspace']]
with s.claim(resources) as (owner,fds):
    s.transition(t['id'],'routing');s.transition(t['id'],'routed');s.transition(t['id'],'running')
    r=s.record('run',{'status':'running','provider':'codex','owner':owner,'resources':resources},t['id'],p['id'])
    print(r['id'],flush=True)
    os._exit(0)
'''
            result = subprocess.run([sys.executable, "-c", script, str(s.root), ident], stdout=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0)
            run_id = result.stdout.decode().strip()
            self.assertEqual(s.task(ident)["status"], "running")
            with self.assertRaises(Busy):
                with s.claim(["workspace:" + project["workspace"]]):
                    self.fail("stale running operation was replayed")
            s.reconcile_run(run_id)
            self.assertEqual(s.task(ident)["status"], "outcome_unknown")
            with self.assertRaises(Busy):
                with s.claim(["workspace:" + project["workspace"]]):
                    self.fail("inspection alone released unresolved operation")
            s.reconcile_run(run_id, "failed", "operator inspected fixture; no effects")
            with s.claim(["task:" + ident, "workspace:" + project["workspace"]]):
                pass

    def test_second_process_cannot_claim_live_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            s = Store(temp)
            self.addCleanup(s.close)
            script = '''import sys
from work_assistant.store import Store,Busy
s=Store(sys.argv[1])
try:
    with s.claim(['workspace:synthetic']): pass
except Busy: sys.exit(7)
sys.exit(0)
'''
            with s.claim(["workspace:synthetic"]):
                second = subprocess.run([sys.executable, "-c", script, temp], timeout=10)
                self.assertEqual(second.returncode, 7)
