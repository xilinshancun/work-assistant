"""Package-payload onboarding without a source checkout or real configuration."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from work_assistant import cli, evaluation
from work_assistant import __version__
from work_assistant.example import FILES, create_example

ROOT = Path(__file__).resolve().parents[1]


class InstalledOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()

    def test_canonical_package_resources_match_audited_source_examples_byte_for_byte(self):
        target=self.root/'example';result=create_example(target)
        self.assertEqual(result['schema'],'wa.example.v1');self.assertEqual(result['status'],'created')
        self.assertEqual(set(result['files']),set(FILES));self.assertEqual({p.name for p in target.iterdir()},set(FILES))
        canonical=ROOT/'work_assistant'/'resources'/'first_project'
        for name in FILES:
            self.assertEqual((target/name).read_bytes(),(canonical/name).read_bytes())
            self.assertEqual((canonical/name).read_bytes(),(ROOT/'examples'/'first-project'/name).read_bytes())
        self.assertEqual(result['next_steps'][0]['argv'][-2:],['--allow','read'])

    def test_example_refuses_existing_file_directory_and_both_symlink_kinds(self):
        existing=self.root/'existing';existing.mkdir();(existing/'sentinel').write_text('preserve')
        ordinary=self.root/'file';ordinary.write_text('preserve file')
        linked=self.root/'linked';linked.symlink_to(existing,target_is_directory=True)
        broken=self.root/'broken';broken.symlink_to(self.root/'absent',target_is_directory=True)
        for path in (existing,ordinary,linked,broken):
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'new directory'):create_example(path)
        self.assertEqual((existing/'sentinel').read_text(),'preserve');self.assertEqual(ordinary.read_text(),'preserve file')
        self.assertTrue(linked.is_symlink());self.assertTrue(broken.is_symlink())

    def test_missing_installed_resources_has_no_checkout_fallback_or_partial_output(self):
        with patch('work_assistant.example.resources.files',return_value=self.root/'missing-package'):
            with self.assertRaisesRegex(ValueError,'complete Work Assistant installation'):
                create_example(self.root/'not-created')
        self.assertFalse((self.root/'not-created').exists())

    def test_both_cli_commands_run_before_config_or_store_access_and_keep_exit_contract(self):
        for command in ('example','evaluate'):
            output=io.StringIO()
            with patch('work_assistant.cli.config.load',side_effect=AssertionError('no config access')), \
                    patch('work_assistant.cli.Store',side_effect=AssertionError('no real state access')),redirect_stdout(output):
                code=cli.main(['--config',str(self.root/'private-unread-config'),command,'--output',str(self.root/command)])
            self.assertEqual(code,0)
            result=json.loads(output.getvalue())
            if command=='example':self.assertEqual(result['workspace'],str(self.root/command))
            else:
                self.assertEqual(result['scope']['entry_point'],'wa_evaluate')
                self.assertEqual(result['scope']['package_version'], __version__)
                self.assertEqual(result['summary'],{'passed':4,'failed':0,'skipped':0})
        output=io.StringIO()
        with redirect_stdout(output):
            code=cli.main(['evaluate','--output',str(self.root/'evaluate')])
        self.assertEqual(code,1);self.assertEqual(json.loads(output.getvalue())['status'],'failed')

    def test_help_explains_offline_evaluation_and_new_example_destination(self):
        for command,phrase in (('evaluate','NEW directory'),('example','NEW workspace directory')):
            output=io.StringIO()
            with redirect_stdout(output),self.assertRaises(SystemExit) as caught:cli.main([command,'--help'])
            self.assertEqual(caught.exception.code,0);self.assertIn(phrase,' '.join(output.getvalue().split()))

    def test_wheel_style_payload_supports_example_and_evaluate_outside_checkout(self):
        # This exercises an extracted wheel payload/entry wrapper using stdlib;
        # building and installing the actual release wheel belongs to package_smoke.
        archive=self.root/'synthetic-wheel.whl';package=ROOT/'work_assistant'
        with zipfile.ZipFile(archive,'w') as wheel:
            for path in package.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and (path.suffix=='.py' or path.name in FILES):
                    wheel.write(path,'work_assistant/'+path.relative_to(package).as_posix())
        site=self.root/'site';site.mkdir()
        with zipfile.ZipFile(archive) as wheel:wheel.extractall(site)
        entry=self.root/'bin'/'wa';entry.parent.mkdir()
        entry.write_text('from work_assistant.cli import main\nraise SystemExit(main())\n')
        cwd=self.root/'unrelated';cwd.mkdir();home=self.root/'isolated-home';home.mkdir()
        poison=cwd/'work_assistant';poison.mkdir();(poison/'__init__.py').write_text('raise RuntimeError("cwd fallback must not load")')
        (cwd/'config.local.json').write_text('private malformed configuration must not be read')
        env={'HOME':str(home),'PATH':os.defpath,'PYTHONPATH':str(site),'TMPDIR':str(self.root),
             'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','FEISHU_APP_SECRET':'synthetic-unused-secret'}
        def command(*args):
            result=subprocess.run([sys.executable,'-S',str(entry)]+list(args),cwd=cwd,env=env,
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('synthetic-unused-secret',result.stdout+result.stderr)
            return json.loads(result.stdout)
        example=command('example','--output',str(self.root/'generated'))
        workspace=Path(example['workspace']);self.assertEqual({p.name for p in workspace.iterdir()},set(FILES))
        count=subprocess.run([sys.executable,'-S',str(workspace/'count_records.py'),'records.txt'],cwd=workspace,
                             env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(count.returncode,0,count.stderr);self.assertEqual(count.stdout.strip(),'5')
        evaluated=command('evaluate','--output',str(self.root/'receipt'))
        self.assertEqual(evaluated['schema'],'wa.evaluation.v1');self.assertEqual(evaluated['status'],'passed')
        self.assertEqual(evaluated['scope']['entry_point'],'wa_evaluate')
        self.assertIn('loaded work_assistant package',evaluated['scope']['runtime_location'])
        self.assertEqual(evaluated['summary'],{'passed':4,'failed':0,'skipped':0})
        # The evaluator's child environment is pinned to the extracted runtime,
        # not ROOT, the unrelated cwd, or the separately maintained examples.
        probe=entry.parent/'runtime-location.py'
        probe.write_text('import work_assistant.evaluation as e\nprint(e.SOURCE)\n')
        isolated=subprocess.run([sys.executable,'-S',str(probe)],cwd=cwd,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(isolated.returncode,0,isolated.stderr);self.assertEqual(Path(isolated.stdout.strip()),site)
        self.assertFalse((cwd/'.wa').exists());self.assertEqual(list(home.iterdir()),[])
