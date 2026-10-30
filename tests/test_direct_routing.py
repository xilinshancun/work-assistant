"""Explicit host-selected routes; all providers/workspaces are synthetic."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from unittest.mock import patch

from work_assistant import cli
from work_assistant.assistant import ask_task
from work_assistant.policy import validate_route
from work_assistant.process import ProcessResult
from work_assistant.routing import route_task
from work_assistant.runner import run_task
from work_assistant.util import dumps
from test_runtime import Base, FakeProviders, response, route_value


class Executor:
    def __init__(self, conf, status='completed', escalate=False, review_missing=False):
        self.config=conf;self.status=status;self.calls=[];self.escalate=escalate;self.review_missing=review_missing

    def invoke(self, provider, prompt, workspace, **kwargs):
        self.calls.append((provider,kwargs))
        if kwargs.get('classifier'):
            if provider=='dsh' and self.escalate:return response('',status='failed',error='synthetic missing classifier')
            request=json.JSONDecoder().raw_decode(prompt.split('Input: ',1)[1])[0]
            return response(dumps(route_value(request['task_id'],requested_action=request['intent'])))
        if prompt.startswith('Read-only independent review') and self.review_missing:
            return response('',status='failed',error='opposite provider unavailable',launched=False)
        return response('Synthetic executor result',status=self.status,error='' if self.status=='completed' else 'synthetic unavailable executor',launched=False)


class DirectRoutingTests(Base):
    def test_each_direct_executor_runs_once_without_any_classifier_or_planner(self):
        for selected in ('claude_code','codex'):
            providers=Executor(self.conf)
            with patch('work_assistant.routing.needs_escalation',side_effect=AssertionError('no classification score')):
                result=ask_task(self.s,providers,self.p['id'],'Synthetic direct task',provider=selected,routing='direct')
            self.assertEqual(result['status'],'completed');self.assertEqual(result['routing'],'direct')
            self.assertEqual(len(providers.calls),1);self.assertEqual(providers.calls[0][0],selected)
            self.assertFalse(providers.calls[0][1].get('classifier',False))
            self.assertEqual(providers.calls[0][1]['action'],'read')
            data=self.s.records('route',task_id=result['task_id'])[0]['data']
            self.assertEqual((data['stage'],data['provider'],data['mode']),('direct','user_direct','direct'))
            self.assertEqual(data['basis'],'explicit_user_selection');self.assertEqual(data['scoring'],'none (no classifier)')
            self.assertIsNone(data['response']);self.assertEqual(data['fallback_count'],0)
            value=validate_route(data['route'],result['task_id'])
            self.assertEqual(value['executor'],selected);self.assertEqual(value['complexity'],'unknown')
            self.assertEqual(value['confidence'],0.0);self.assertFalse(value['needs_escalation'])
            self.assertIn('explicitly selected',value['reason'])
            self.assertEqual(self.s.task(result['task_id'])['goal_status'],'unverified')
            events=self.s.records('event',task_id=result['task_id'])
            self.assertEqual(sum(e['data']['action']=='direct_route' for e in events),1)

    def test_invalid_direct_arguments_create_no_task_and_cli_never_opens_config_or_store(self):
        providers=Executor(self.conf)
        for selected,routing in ((None,'direct'),('dsh','direct'),('codex','invalid'),('codex',True)):
            with self.assertRaises(ValueError):
                ask_task(self.s,providers,self.p['id'],'Synthetic invalid task',provider=selected,routing=routing)
        self.assertEqual(self.s.tasks(),[]);self.assertEqual(providers.calls,[])
        with patch('work_assistant.cli.config.load',side_effect=AssertionError('no config access')), \
                patch('work_assistant.cli.Store',side_effect=AssertionError('no state access')),redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:cli.main(['ask','synthetic','goal','--routing','direct'])
        self.assertEqual(caught.exception.code,2)
        with redirect_stderr(io.StringIO()):
            for args in (['task','add','project','goal','--routing','direct'],
                         ['route','task','--routing','direct','--provider','claude_code']):
                with self.assertRaises(SystemExit):cli.parser().parse_args(args)

    def test_existing_unpinned_task_cannot_get_a_direct_provider_or_replacement_task(self):
        ident=self.task();providers=Executor(self.conf)
        with self.assertRaisesRegex(ValueError,'explicitly selected'):route_task(self.s,providers,ident,routing='direct')
        self.assertEqual(self.s.task(ident)['status'],'queued');self.assertEqual(len(self.s.tasks()),1)
        self.assertEqual(self.s.records('route'),[]);self.assertEqual(providers.calls,[])

    def test_denied_read_or_write_grants_create_no_execution_or_new_authority(self):
        for intent,grants in (('read',[]),('write_workspace',['read'])):
            project=self.s.add_project('denied-'+intent,self.root,grants);providers=Executor(self.conf)
            result=ask_task(self.s,providers,project['id'],'Synthetic denied ask',intent=intent,provider='claude_code',routing='direct')
            self.assertEqual(result['status'],'needs_user');self.assertEqual(providers.calls,[])
            task=self.s.add_task(project['id'],'Synthetic denied route',intent,'codex')
            routed=route_task(self.s,providers,task['id'],routing='direct')
            self.assertFalse(routed['data']['accepted']);self.assertEqual(self.s.task(task['id'])['status'],'needs_user')
            self.assertEqual(self.s.project(project['id'])['grants'],grants)
            self.assertEqual(self.s.records('run',task_id=task['id']),[])

    def test_forbidden_states_are_rejected_before_any_route_or_model(self):
        providers=Executor(self.conf)
        for state in ('running','collecting','outcome_unknown','cancelled','routing'):
            ident=self.task(provider='claude_code');self.s.db.execute('UPDATE tasks SET status=? WHERE id=?',(state,ident))
            for mode in ('auto','direct'):
                with self.assertRaisesRegex(ValueError,'cannot be routed'):route_task(self.s,providers,ident,routing=mode)
            self.assertEqual(self.s.records('route',task_id=ident),[])
        self.assertEqual(providers.calls,[])

    def test_state_change_under_claim_is_rechecked_before_routing(self):
        providers=Executor(self.conf);original=self.s.claim
        for state in ('running','collecting','outcome_unknown','cancelled','routing'):
            ident=self.task(provider='claude_code')
            @contextmanager
            def claim(resources):
                with original(resources) as held:
                    self.s.db.execute('UPDATE tasks SET status=? WHERE id=?',(state,ident))
                    yield held
            with patch.object(self.s,'claim',claim):
                with self.assertRaisesRegex(ValueError,'cannot be routed'):route_task(self.s,providers,ident,routing='direct')
            self.assertEqual(self.s.task(ident)['status'],state);self.assertEqual(self.s.records('route',task_id=ident),[])
        self.assertEqual(providers.calls,[])

    def test_fresh_intent_and_revoked_grants_are_used_inside_claim(self):
        project=self.s.add_project('revoked',self.root,['read']);task=self.s.add_task(project['id'],'Synthetic revocation',provider='claude_code')
        providers=Executor(self.conf);original=self.s.claim
        @contextmanager
        def claim(resources):
            with original(resources) as held:
                self.s.db.execute('UPDATE tasks SET intent=? WHERE id=?',('write_workspace',task['id']))
                self.s.db.execute('UPDATE projects SET grants=? WHERE id=?',('[]',project['id']))
                yield held
        with patch.object(self.s,'claim',claim):routed=route_task(self.s,providers,task['id'],routing='direct')
        self.assertEqual(routed['data']['route']['requested_action'],'write_workspace');self.assertFalse(routed['data']['accepted'])
        self.assertEqual(self.s.task(task['id'])['status'],'needs_user');self.assertEqual(providers.calls,[])

    def test_workspace_disappearing_under_claim_cannot_create_a_direct_route(self):
        workspace=self.root/'vanishing';workspace.mkdir()
        project=self.s.add_project('vanishing',workspace,['read'])
        task=self.s.add_task(project['id'],'Synthetic vanished workspace',provider='claude_code')
        original=self.s.claim;providers=Executor(self.conf)
        @contextmanager
        def claim(resources):
            with original(resources) as held:
                workspace.rmdir()
                yield held
        with patch.object(self.s,'claim',claim):
            with self.assertRaisesRegex(ValueError,'workspace no longer exists'):
                route_task(self.s,providers,task['id'],routing='direct')
        self.assertEqual(self.s.task(task['id'])['status'],'queued');self.assertEqual(providers.calls,[])

    def test_direct_write_keeps_intent_and_partial_failed_write_becomes_unknown(self):
        providers=Executor(self.conf,status='failed');invoke=providers.invoke
        def partial_write(*args,**kwargs):
            return dict(invoke(*args,**kwargs),launched=True)
        providers.invoke=partial_write
        result=ask_task(self.s,providers,self.p['id'],'Synthetic partial write',intent='write_workspace',
                        provider='claude_code',routing='direct')
        self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(len(providers.calls),1)
        self.assertEqual(providers.calls[0][1]['action'],'write_workspace')
        self.assertEqual(self.s.records('route',task_id=result['task_id'])[0]['data']['route']['requested_action'],'write_workspace')
        with self.assertRaises(ValueError):route_task(self.s,providers,result['task_id'],routing='direct')
        self.assertEqual(len(providers.calls),1)

    def test_direct_always_records_a_new_decision_and_key_ignores_provider_configuration(self):
        ident=self.task(provider='codex');providers=Executor(self.conf)
        first=route_task(self.s,providers,ident,routing='direct')
        self.conf['providers']['codex']['argv']=['changed-synthetic-cli']
        second=route_task(self.s,providers,ident,routing='direct')
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(first['data']['cache_key'],second['data']['cache_key'])
        self.assertEqual(providers.calls,[])

    def test_auto_cache_legacy_auto_and_cross_mode_switch_use_latest_accepted_route(self):
        ident=self.task()
        providers=FakeProviders(self.conf,[response(dumps(route_value(ident,executor='codex'))),
            response(dumps(route_value(ident,executor='codex'))),response('Synthetic latest auto execution')])
        first=route_task(self.s,providers,ident);self.assertEqual(route_task(self.s,providers,ident)['id'],first['id'])
        # A pre-mode route remains a valid auto cache entry.
        legacy=dict(first['data']);legacy.pop('mode');self.s.update_record(first['id'],legacy)
        self.assertEqual(route_task(self.s,providers,ident)['id'],first['id']);self.assertEqual(len(providers.calls),1)
        # Synthetic trusted task edits make the old/new auto request identical,
        # while the intervening explicit route selects a different executor.
        self.s.db.execute('UPDATE tasks SET provider=? WHERE id=?',('claude_code',ident))
        direct=route_task(self.s,providers,ident,routing='direct')
        self.s.db.execute('UPDATE tasks SET provider=NULL WHERE id=?',(ident,))
        latest=route_task(self.s,providers,ident)
        self.assertNotEqual(latest['id'],first['id']);self.assertNotEqual(latest['id'],direct['id'])
        self.assertEqual(latest['data']['mode'],'auto');self.assertEqual(len(providers.calls),2)
        self.assertEqual(route_task(self.s,providers,ident)['id'],latest['id'])
        run=run_task(self.s,providers,ident)
        self.assertEqual(run['data']['provider'],'codex')
        self.assertEqual(providers.calls[-1][0][0],'codex');self.assertFalse(providers.calls[-1][1].get('classifier',False))

    def test_default_auto_provider_selection_still_classifies_and_escalates(self):
        providers=Executor(self.conf,escalate=True)
        result=ask_task(self.s,providers,self.p['id'],'Synthetic auto task',provider='claude_code')
        self.assertEqual(result['status'],'completed')
        self.assertEqual([provider for provider,_ in providers.calls],['dsh','codex','claude_code'])
        self.assertTrue(all(kwargs.get('classifier') for _,kwargs in providers.calls[:2]))

    def test_unknown_is_not_rerouted_or_replayed_and_failed_run_has_mode_aware_doctor_guidance(self):
        for status in ('failed','outcome_unknown'):
            providers=Executor(self.conf,status=status)
            result=ask_task(self.s,providers,self.p['id'],'Synthetic incomplete task',provider='claude_code',routing='direct')
            self.assertEqual(result['status'],status);self.assertEqual(len(providers.calls),1)
            doctor=next(step for step in result['next_steps'] if step['action']=='check_provider_readiness')
            self.assertEqual(doctor['argv'][-2:],['doctor','--json']);self.assertIn('Direct routing',doctor['note'])
            if status=='outcome_unknown':
                with self.assertRaises(ValueError):route_task(self.s,providers,result['task_id'],routing='direct')
                with self.assertRaises(ValueError):run_task(self.s,providers,result['task_id'])
                self.assertEqual(len(providers.calls),1)

    def test_missing_opposite_reviewer_cannot_fake_independent_success(self):
        providers=Executor(self.conf,review_missing=True)
        original=run_task
        def synthetic_observation(*args,**kwargs):
            return original(*args,observe=lambda *_a,**_k:ProcessResult('synthetic git observation'),**kwargs)
        with patch('work_assistant.assistant.run_task',side_effect=synthetic_observation):
            result=ask_task(self.s,providers,self.p['id'],'Synthetic reviewed task',provider='claude_code',routing='direct',review=True)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['task_status'],'completed')
        self.assertEqual(result['goal_status'],'unverified');self.assertFalse(self.s.task(result['task_id'])['verified'])
        self.assertEqual([provider for provider,_ in providers.calls],['claude_code','codex'])
        self.assertEqual(self.s.records('report',task_id=result['task_id']),[])

    def test_cli_ask_and_route_direct_use_explicit_existing_provider(self):
        path=self.root/'cli-config.json';path.write_text(dumps({'state_dir':str(self.s.root)}))
        providers=Executor(self.conf);output=io.StringIO()
        with patch('work_assistant.cli.Providers',return_value=providers),redirect_stdout(output):
            code=cli.main(['--config',str(path),'ask',self.p['id'],'Synthetic CLI task','--routing','direct','--provider','claude_code'])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['status'],'completed')
        self.assertEqual(len(providers.calls),1)
        ident=self.task(provider='codex');output=io.StringIO()
        with patch('work_assistant.cli.Providers',return_value=providers),redirect_stdout(output):
            code=cli.main(['--config',str(path),'route',ident,'--routing','direct'])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['data']['mode'],'direct')
        self.assertEqual(self.s.task(ident)['status'],'routed');self.assertEqual(len(providers.calls),1)
