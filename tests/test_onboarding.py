"""Clean-home onboarding and truthful local readiness; synthetic providers only."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from work_assistant import __version__, config
from work_assistant.assistant import ask_task
from work_assistant.process import ProcessResult
from work_assistant.util import dumps
from test_ask import AskProvider
from test_runtime import Base


HELP = {'dsh': '--profile',
        'codex': '--json --sandbox --output-schema --output-last-message',
        'claude_code': '--output-format --permission-mode --tools --resume'}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.conf=config.load(self.root/'config.json');self.calls=[]
        for name,item in self.conf['providers'].items():item['argv']=['fixture-'+name]

    def process(self,argv,prompt,**kwargs):
        self.calls.append((argv,prompt,kwargs))
        self.assertEqual(prompt,'')
        if argv[-3:]==['mcp','list','--json']:return ProcessResult('[]')
        self.assertEqual(argv[-1],'--help')
        return ProcessResult(HELP[argv[0].replace('fixture-','')])

    def test_missing_optional_and_required_providers_have_targeted_next_steps_without_processes(self):
        with patch('work_assistant.config.shutil.which',return_value=None):result=config.doctor(self.conf,self.process)
        self.assertEqual(self.calls,[])
        for name in HELP:
            self.assertFalse(result[name]['installed']);self.assertEqual(result[name]['readiness'],'not_installed')
            self.assertEqual(result[name]['authentication'],'not tested');self.assertTrue(result[name]['next_steps'])
        self.assertIn('optional',' '.join(result['dsh']['next_steps']))
        self.assertIn('fallback',' '.join(result['dsh']['next_steps']))
        workflows=result['workflows']
        self.assertEqual(workflows['offline']['readiness'],'available')
        self.assertEqual(workflows['offline']['authentication'],'not required')
        self.assertEqual(workflows['ask']['readiness'],'needs_providers');self.assertEqual(workflows['ask']['paths'],[])
        self.assertTrue(workflows['independent_review']['optional']);self.assertTrue(workflows['feishu_live']['optional'])

    def test_valid_local_capabilities_never_claim_authenticated_or_live_ready(self):
        with patch('work_assistant.config.shutil.which',return_value='synthetic'):
            result=config.doctor(self.conf,self.process)
        for name in HELP:
            self.assertTrue(result[name]['installed']);self.assertEqual(result[name]['readiness'],'local_checks_passed')
            self.assertEqual(result[name]['authentication'],'not tested')
            self.assertIn('model access untested',result[name]['diagnostic'])
        self.assertEqual(sum(argv[-3:]==['mcp','list','--json'] for argv,_,_ in self.calls),2)
        self.assertEqual(result['workflows']['ask']['readiness'],'local_checks_passed')
        self.assertEqual(result['workflows']['ask']['authentication'],'not tested')

    def test_codex_alone_provides_an_ask_path_without_optional_dsh_claude_or_feishu(self):
        with patch('work_assistant.config.shutil.which',side_effect=lambda executable,**_: 'synthetic' if executable=='fixture-codex' else None), \
                patch.dict(os.environ,{},clear=True):
            result=config.doctor(self.conf,self.process)
        self.assertFalse(result['dsh']['installed']);self.assertFalse(result['claude_code']['installed'])
        self.assertEqual(result['workflows']['ask']['readiness'],'local_checks_passed')
        self.assertEqual([p['provider'] for p in result['workflows']['ask']['paths']],['codex'])
        self.assertEqual(result['workflows']['independent_review']['readiness'],'needs_providers')
        self.assertEqual(result['workflows']['feishu_live']['readiness'],'not_configured')
        self.assertEqual(result['workflows']['offline']['readiness'],'available')

    def test_claude_alone_cannot_route_and_dsh_claude_is_only_a_limited_direct_path(self):
        for installed, expected in (({'fixture-claude_code'},'needs_providers'),
                                    ({'fixture-claude_code','fixture-dsh'},'limited_direct_route'),
                                    ({'fixture-claude_code','fixture-codex'},'local_checks_passed')):
            with self.subTest(installed=installed),patch('work_assistant.config.shutil.which',
                    side_effect=lambda executable,**_: 'synthetic' if executable in installed else None):
                result=config.doctor(self.conf,self.process)
            workflow=result['workflows']['ask']
            self.assertEqual(workflow['readiness'],expected)
            self.assertEqual(workflow['gpt_fallback'],'local_checks_passed' if 'fixture-codex' in installed else 'unavailable')
            if expected=='needs_providers':self.assertEqual(workflow['paths'],[])
            elif expected=='limited_direct_route':self.assertEqual(workflow['paths'][0]['coverage'],'direct_classification_only')
            else:
                claude=next(path for path in workflow['paths'] if path['provider']=='claude_code')
                self.assertEqual(claude['coverage'],'planner_and_executor')

    def test_missing_help_flag_is_incompatible_even_when_mcp_checks_pass(self):
        def process(argv,*args,**kwargs):
            if argv[0]=='fixture-codex' and argv[-1]=='--help':return ProcessResult('--json --sandbox --output-last-message')
            return self.process(argv,*args,**kwargs)
        with patch('work_assistant.config.shutil.which',return_value='synthetic'):
            result=config.doctor(self.conf,process)
        self.assertEqual(result['codex']['readiness'],'incompatible')
        self.assertIn('--output-schema',result['codex']['diagnostic'])
        self.assertTrue(result['codex']['capabilities']['mcp_isolation_ready'])

    def test_help_launch_failure_does_not_abort_other_diagnostics_or_echo_private_output(self):
        self.conf['providers']['codex']['env']={'SYNTHETIC_API_TOKEN':'private-credential-fixture'}
        def process(argv,*args,**kwargs):
            if argv[0]=='fixture-codex':raise OSError('private-credential-fixture')
            return self.process(argv,*args,**kwargs)
        with patch('work_assistant.config.shutil.which',return_value='synthetic'):
            result=config.doctor(self.conf,process)
        self.assertEqual(result['codex']['readiness'],'probe_failed')
        self.assertEqual(result['dsh']['readiness'],'local_checks_passed')
        self.assertIn('feishu',result);self.assertNotIn('private-credential-fixture',dumps(result))

    def test_failed_truncated_timeout_and_invalid_utf8_help_are_not_available(self):
        for response in (ProcessResult('--profile',returncode=1),ProcessResult('--profile',timed_out=True),
                         ProcessResult('--profile',truncated=True),ProcessResult('--profile',invalid_utf8=True)):
            with self.subTest(response=response):
                def process(argv,*args,**kwargs):
                    return response if argv[0]=='fixture-dsh' else self.process(argv,*args,**kwargs)
                with patch('work_assistant.config.shutil.which',return_value='synthetic'):
                    result=config.doctor(self.conf,process)
                self.assertEqual(result['dsh']['readiness'],'probe_failed')

    def test_provider_path_overlay_controls_discovery_like_the_child_environment(self):
        binary=self.root/'bin';binary.mkdir();executable=binary/'fixture-codex'
        executable.write_text('synthetic executable; injected process only');executable.chmod(0o700)
        self.conf['providers']['codex']['env']['PATH']=str(binary)
        with patch.dict(os.environ,{'PATH':''}):result=config.doctor(self.conf,self.process)
        self.assertTrue(result['codex']['installed']);self.assertEqual(result['codex']['readiness'],'local_checks_passed')
        self.assertFalse(result['dsh']['installed'])
        self.assertTrue(all(kwargs['env']['PATH']==str(binary) for _,_,kwargs in self.calls))

    def test_publisher_only_doctor_configuration_keeps_offline_paths_without_provider_keys(self):
        conf=copy.deepcopy(self.conf);conf['providers']={}
        conf['publisher'].update(transport='lark_cli',expected_app_id='cli_fixture',cli_argv=['synthetic-lark'],cli_config_dir=str(self.root))
        with patch('work_assistant.lark_cli.LarkCLI.diagnose',return_value={'configured':True,'live_tested':False}):
            result=config.doctor(conf,self.process)
        self.assertEqual(self.calls,[]);self.assertTrue(result['feishu']['configured'])
        self.assertEqual(result['workflows']['offline']['readiness'],'available')
        self.assertEqual(result['workflows']['ask']['readiness'],'needs_providers')


class AskGuidanceTests(Base):
    def test_missing_provider_route_guidance_preserves_existing_task_and_calls_no_extra_models(self):
        providers=AskProvider(self.conf,route_status='failed')
        result=ask_task(self.s,providers,self.p['id'],'Synthetic setup failure')
        self.assertEqual(result['status'],'needs_user');self.assertEqual(len(providers.calls),2)
        self.assertEqual([step['action'] for step in result['next_steps']],['inspect_task','check_provider_readiness'])
        self.assertEqual(result['next_steps'][0]['argv'][-3:],['task','show',result['task_id']])
        self.assertEqual(result['next_steps'][1]['argv'][-2:],['doctor','--json'])
        self.assertEqual(len(self.s.tasks()),1)

    def test_unknown_guidance_only_inspects_existing_run_without_resume_or_retry(self):
        providers=AskProvider(self.conf,run_status='outcome_unknown')
        result=ask_task(self.s,providers,self.p['id'],'Synthetic unknown outcome')
        self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(len(providers.calls),2)
        self.assertEqual(self.s.task(result['task_id'])['status'],'outcome_unknown')
        run=self.s.records('run',task_id=result['task_id'])[0]
        self.assertEqual(result['next_steps'][-1]['argv'][-2:],['reconcile',run['id']])
        self.assertFalse(any(word in step['argv'] for step in result['next_steps'] for word in ('ask','run','--followup')))


class PackageLayoutTests(unittest.TestCase):
    def test_package_only_layout_from_unrelated_cwd_with_clean_home_runs_entire_onboarding(self):
        # Runtime installation boundary: only package files, no repo/docs/tests or
        # editable-import path. Wheel construction is checked by packaging CI.
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);site=root/'site';site.mkdir();cwd=root/'unrelated';cwd.mkdir()
            package=Path(__file__).resolve().parents[1]/'work_assistant'
            shutil.copytree(package,site/'work_assistant',ignore=shutil.ignore_patterns('__pycache__'))
            home=root/'home';home.mkdir();settings=root/'settings'/'config.json';settings.parent.mkdir()
            env={'HOME':str(home),'PATH':os.defpath,'TMPDIR':str(root),'PYTHONPATH':str(site),
                 'PYTHONPYCACHEPREFIX':str(root/'cache')}
            def cli(*args,code=0):
                result=subprocess.run([sys.executable,'-m','work_assistant']+list(args),cwd=cwd,env=env,
                                      capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,code,result.stderr)
                return result.stdout if args==('--version',) else json.loads(result.stdout)
            self.assertEqual(cli('--version').strip(),__version__)
            self.assertEqual(cli('demo','--output',str(root/'demo'))['preview'],'dry_run')
            self.assertEqual(cli('--config',str(settings),'init')['status'],'initialized')
            raw=json.loads(settings.read_text())
            for name,item in raw['providers'].items():item['argv']=[str(root/('missing-'+name))]
            settings.write_text(json.dumps(raw))
            project=cli('--config',str(settings),'project','add','synthetic','--workspace','../unrelated')
            self.assertEqual(Path(project['workspace']),cwd.resolve())
            result=cli('--config',str(settings),'ask','synthetic','Synthetic general inspection','--provider','codex',code=2)
            self.assertEqual(result['status'],'needs_user');self.assertTrue(result['next_steps'])
            doctor=cli('--config',str(settings),'doctor','--json')
            self.assertEqual(doctor['codex']['readiness'],'not_installed')
            self.assertEqual(doctor['workflows']['offline']['readiness'],'available')
            # Suggested inspector remains executable with this installed layout.
            inspected=subprocess.run(result['next_steps'][0]['argv'],cwd=cwd,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(inspected.returncode,0,inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)['task']['id'],result['task_id'])
            self.assertFalse((cwd/'.wa').exists());self.assertTrue((settings.parent/'.wa').is_dir())
