"""Synthetic official CLI reaction fixtures; no account, API or listener."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from work_assistant import inbound, reactions
from work_assistant.process import ProcessResult
from work_assistant.store import Busy, Store
from work_assistant.util import digest, dumps
import test_inbound


def reaction(ident="reaction_synthetic", operator="cli_fixture", kind="app", emoji="Get"):
    return {"reaction_id": ident, "operator": {"operator_id": operator, "operator_type": kind},
            "action_time": "1700000000000", "reaction_type": {"emoji_type": emoji}}


class FakeCLI:
    def __init__(self, case):
        self.case=case;self.calls=[];self.items=[];self.source_changes={};self.pages=None
        self.post_error=None;self.apply=True;self.ack_changes={};self.list_error=False
        self.post_list_error=False

    def __call__(self, argv, stdin, **kwargs):
        args=argv[3:];self.calls.append((args,stdin))
        self.case.assertEqual(argv[:3],['synthetic-lark','--profile','fixture'])
        self.case.assertFalse(kwargs['inherit_env'])
        if args==['config','show']:
            return ProcessResult(dumps({'appId':'cli_fixture','brand':'feishu'}))
        self.case.assertEqual(args[0],'api');self.case.assertEqual(args[args.index('--as')+1],'bot')
        method,path=args[1:3]
        if path=='/open-apis/bot/v3/info':data={'open_id':'ou_bot'}
        elif path=='/open-apis/im/v1/messages/om_source':
            data={'items':[dict({'message_id':'om_source','chat_id':'oc_fixture','deleted':False,
                'body':{'content':'private-source-marker-never-persisted'}},**self.source_changes)]}
        elif path=='/open-apis/im/v1/messages/om_source/reactions':
            if method=='POST':
                body=json.loads(stdin);self.case.assertEqual(set(body),{'reaction_type'})
                record=next(r['data'] for r in self.case.store.records(reactions.KIND) if r['data']['status']=='sending')
                self.case.assertEqual(record['status'],'sending')
                self.case.assertEqual(record['post_intent']['body_hash'],digest(body))
                self.case.assertEqual(record['post_intent']['path'],'/im/v1/messages/om_source/reactions')
                self.case.assertEqual(args[-2:],['--data','-']);self.case.assertNotIn('--params',args)
                item=reaction('reaction_synthetic'+('_'+str(len(self.posts())) if len(self.posts())>1 else ''),
                              emoji=body['reaction_type']['emoji_type'])
                if self.apply:self.items.append(item)
                if self.post_error=='interrupt':raise KeyboardInterrupt()
                if self.post_error=='timeout':return ProcessResult(timed_out=True)
                if self.post_error=='definite':
                    return ProcessResult('',dumps({'ok':False,'identity':'bot','error':{
                        'type':'authorization','subtype':'permission_denied','retryable':False}}),returncode=1)
                data=dict(item,**self.ack_changes)
            else:
                self.case.assertEqual(method,'GET')
                params=json.loads(args[args.index('--params')+1])
                self.case.assertEqual(params['page_size'],'50')
                self.case.assertIn(params['reaction_type'],reactions.EMOJIS)
                if self.list_error or (self.post_list_error and self.posts()):return ProcessResult(timed_out=True)
                data=self.pages[int(params.get('page_token','0'))] if self.pages is not None else {'items':self.items,'has_more':False}
        else:raise AssertionError('unexpected synthetic API path')
        return ProcessResult(dumps({'ok':True,'identity':'bot','data':data}))

    def posts(self):return [c for c in self.calls if c[0][:2]==['api','POST']]


class TimedCLI:
    """Deterministic subprocess durations that honor the passed timeout."""
    def __init__(self, cli, durations):
        self.cli=cli;self.durations=iter(durations);self.now=0;self.timeouts=[]

    def clock(self):return self.now

    def __call__(self, argv, stdin, **kwargs):
        duration=next(self.durations)
        self.timeouts.append(kwargs['timeout'])
        self.now+=min(duration,kwargs['timeout'])
        if duration>kwargs['timeout']:
            # Record the launched call even though its outcome is unavailable.
            self.cli.calls.append((argv[3:],stdin))
            return ProcessResult(timed_out=True)
        return self.cli(argv,stdin,**kwargs)


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.settings={'mode':'app','transport':'lark_cli','expected_app_id':'cli_fixture',
            'cli_argv':['synthetic-lark'],'cli_profile':'fixture','cli_config_dir':str(self.root)}
        self.cli=FakeCLI(self)
        timer=patch('work_assistant.lark_cli.time.sleep');timer.start();self.addCleanup(timer.stop)

    def add(self, request='synthetic-reaction', emoji='Get', **kwargs):
        return reactions.add(self.store,self.settings,request,'oc_fixture','om_source',emoji,confirm=True,process=self.cli,**kwargs)

    def inspect(self,result):return reactions.inspect(self.store,self.settings,result['action_id'],process=self.cli)

    def test_success_exact_app_emoji_and_same_reaction_id_then_repeat_noop(self):
        result=self.add();self.assertEqual(result['status'],'completed');self.assertEqual(result['reaction_id'],'reaction_synthetic')
        self.assertEqual(result['verification_status'],'verified');self.assertFalse(result['adopted_existing'])
        count=len(self.cli.calls);self.assertTrue(self.add()['no_op']);self.assertEqual(len(self.cli.calls),count)
        self.assertEqual(self.inspect(result)['status'],'completed');self.assertEqual(len(self.cli.posts()),1)
        self.assertTrue(all(call[0][1]=='GET' for call in self.cli.calls[count:] if call[0][0]=='api'))
        self.assertNotIn('private-source-marker-never-persisted',dumps(self.store.records()))

    def test_existing_app_emoji_adopted_after_all_pages_without_post(self):
        self.cli.pages=[{'items':[reaction('other_app','cli_other'),reaction('human','ou_owner','user')],
                         'has_more':True,'page_token':'1'},
                        {'items':[reaction('own_existing')],'has_more':False}]
        result=self.add();self.assertEqual(result['status'],'completed');self.assertEqual(result['reaction_id'],'own_existing')
        self.assertTrue(result['adopted_existing']);self.assertIsNone(result['post_intent']);self.assertEqual(self.cli.posts(),[])
        result=reactions.list_reactions(self.settings,'oc_fixture','om_source',process=self.cli)
        self.assertEqual([r['reaction_id'] for r in result['items']],['own_existing'])

    def test_other_operator_or_emoji_does_not_count_as_own_acknowledgement(self):
        self.cli.items=[reaction('wrong_emoji',emoji='OK'),reaction('other_app','cli_other'),reaction('human','ou_owner','user')]
        result=self.add();self.assertEqual(result['status'],'completed');self.assertEqual(len(self.cli.posts()),1)

    def test_unknown_applied_is_adopted_by_readonly_inspect_without_post_replay(self):
        self.cli.post_error='timeout';result=self.add();self.assertEqual(result['status'],'outcome_unknown')
        count=len(self.cli.calls);self.assertTrue(self.add()['no_op']);self.assertEqual(len(self.cli.calls),count)
        with self.assertRaises(reactions.ReactionError):self.add('new-id-must-not-bypass-unknown')
        result=self.inspect(result);self.assertEqual(result['status'],'completed');self.assertEqual(len(self.cli.posts()),1)

    def test_unknown_not_observed_or_definite_post_failure_never_retried(self):
        for index, error in enumerate(('timeout','definite')):
            self.cli.post_error=error;self.cli.apply=False
            result=self.add('unknown-'+str(index),emoji='Get' if index==0 else 'OK')
            self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(self.inspect(result)['status'],'outcome_unknown')
            self.assertTrue(self.add('unknown-'+str(index),emoji='Get' if index==0 else 'OK')['no_op'])
        self.assertEqual(len(self.cli.posts()),2)

    def test_post_readback_failure_keeps_known_id_and_requires_that_exact_id(self):
        self.cli.post_list_error=True;result=self.add()
        self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(result['reaction_id'],'reaction_synthetic')
        self.cli.post_list_error=False;self.cli.items=[reaction('different_id')]
        self.assertEqual(self.inspect(result)['status'],'outcome_unknown')
        self.cli.items.append(reaction());self.assertEqual(self.inspect(result)['status'],'completed')
        self.assertEqual(len(self.cli.posts()),1)

    def test_verified_history_survives_unavailable_or_removed_reaction_with_explicit_new_observation(self):
        result=self.add();original=copy.deepcopy(result)
        self.cli.list_error=True
        unavailable=self.inspect(result)
        self.assertEqual(unavailable['status'],'completed');self.assertEqual(unavailable['verification_status'],'verified')
        self.assertFalse(unavailable['last_readback']['confirmed']);self.assertEqual(unavailable['last_readback']['status'],'unavailable')
        self.cli.list_error=False;self.cli.items=[]
        absent=self.inspect(result)
        self.assertEqual(absent['status'],'completed');self.assertEqual(absent['verification_status'],'verified')
        self.assertFalse(absent['last_readback']['confirmed']);self.assertEqual(absent['last_readback']['status'],'not_observed')
        for current in (unavailable,absent):
            self.assertEqual(current['reaction_id'],original['reaction_id'])
            self.assertEqual(current['verified_at'],original['verified_at'])
            self.assertEqual(current['verification_basis'],original['verification_basis'])
        observations=list(reversed(self.store.records('reaction_observation')))
        self.assertEqual([r['data']['readback']['status'] for r in observations],['unavailable','not_observed'])
        self.assertEqual(len(self.cli.posts()),1)

    def test_adopted_verified_history_is_not_reopened_when_reaction_disappears(self):
        self.cli.items=[reaction('existing')];result=self.add();self.assertTrue(result['adopted_existing'])
        self.cli.items=[];result=self.inspect(result)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['verification_status'],'verified')
        self.assertFalse(result['last_readback']['confirmed']);self.assertEqual(self.cli.posts(),[])

    def test_wrong_ack_identity_or_emoji_is_unknown_until_matching_list_observation(self):
        for index, change in enumerate(({'operator':{'operator_type':'app','operator_id':'cli_other'}},
                                        {'reaction_type':{'emoji_type':'OnIt'}})):
            self.cli.ack_changes=change
            result=self.add('bad-ack-'+str(index),emoji='Get' if index==0 else 'OK')
            self.assertEqual(result['status'],'outcome_unknown');self.assertIsNone(result['reaction_id'])
            self.assertEqual(self.inspect(result)['status'],'completed')
        self.assertEqual(len(self.cli.posts()),2)

    def test_source_and_list_preflight_fail_without_post(self):
        for index, change in enumerate(({'chat_id':'oc_other'},{'message_id':'om_wrong'},{'deleted':True})):
            self.cli.source_changes=change;result=self.add('bad-source-'+str(index))
            self.assertEqual(result['status'],'failed');self.assertIsNone(result['post_intent'])
        self.cli.source_changes={};self.cli.list_error=True
        self.assertEqual(self.add('failed-list')['status'],'failed');self.assertEqual(self.cli.posts(),[])

    def test_one_budget_caps_each_preflight_call_and_never_starts_post_on_timeout(self):
        timed=TimedCLI(self.cli,[1,2,3,5])
        result=reactions.add(self.store,self.settings,'bounded-preflight','oc_fixture','om_source',
                             confirm=True,process=timed,budget_s=10,clock=timed.clock)
        self.assertEqual(timed.timeouts,[10,9,7,4])
        self.assertEqual(result['status'],'failed');self.assertIsNone(result['post_intent'])
        self.assertEqual(result['diagnostic'],{'phase':'list','error_type':'timeout','elapsed_ms':10000,'budget_s':10})
        self.assertEqual(self.cli.posts(),[])
        calls=len(self.cli.calls)
        self.assertTrue(reactions.add(self.store,self.settings,'bounded-preflight','oc_fixture','om_source',
            confirm=True,process=timed,budget_s=10,clock=timed.clock)['no_op'])
        self.assertEqual(len(self.cli.calls),calls)

    def test_deadline_after_successful_preflight_prevents_even_post_intent(self):
        timed=TimedCLI(self.cli,[1,2,3,4])
        result=reactions.add(self.store,self.settings,'exact-preflight','oc_fixture','om_source',
                             confirm=True,process=timed,budget_s=10,clock=timed.clock)
        self.assertEqual(result['status'],'failed');self.assertIsNone(result['post_intent'])
        self.assertEqual(result['diagnostic']['error_type'],'budget_exhausted');self.assertEqual(self.cli.posts(),[])

    def test_post_and_readback_deadlines_preserve_unknown_without_second_post(self):
        for index, duration in enumerate((20,6)):
            cli=FakeCLI(self);timed=TimedCLI(cli,[1,1,1,1,duration])
            request='bounded-post-'+str(index)
            emoji='Get' if index==0 else 'OK'
            result=reactions.add(self.store,self.settings,request,'oc_fixture','om_source',emoji,
                                 confirm=True,process=timed,budget_s=10,clock=timed.clock)
            self.assertEqual(result['status'],'outcome_unknown');self.assertIsNotNone(result['post_intent'])
            self.assertEqual(result['diagnostic']['phase'],'post' if index==0 else 'readback')
            self.assertEqual(result['diagnostic']['error_type'],'timeout' if index==0 else 'budget_exhausted')
            self.assertEqual(result['diagnostic']['elapsed_ms'],10000)
            self.assertEqual(len(cli.posts()),1)
            self.assertTrue(reactions.add(self.store,self.settings,request,'oc_fixture','om_source',emoji,
                confirm=True,process=timed,budget_s=10,clock=timed.clock)['no_op'])
            restored=reactions.inspect(self.store,self.settings,result['action_id'],process=cli)
            self.assertEqual(restored['status'],'outcome_unknown' if index==0 else 'completed')
            self.assertEqual(len(cli.posts()),1)

    def test_budget_is_optional_and_final_diagnostics_exclude_raw_errors(self):
        timed=TimedCLI(self.cli,[1,1,1,1,1,1])
        result=reactions.add(self.store,self.settings,'default-timeouts','oc_fixture','om_source',
                             confirm=True,process=timed,clock=timed.clock)
        self.assertEqual(result['status'],'completed');self.assertEqual(timed.timeouts,[30]*6)
        self.assertIsNone(result['diagnostic']['budget_s'])
        def broken(argv,stdin,**kwargs):
            if argv[3:]==['config','show']:return self.cli(argv,stdin,**kwargs)
            raise OSError('secret-marker-do-not-store')
        failed=reactions.add(self.store,self.settings,'diagnostic-synthetic','oc_fixture','om_source','OK',
                             confirm=True,process=broken)
        self.assertEqual(failed['diagnostic']['phase'],'bot_authentication')
        self.assertEqual(failed['diagnostic']['error_type'],'os_error')
        self.assertNotIn('secret-marker',dumps(self.store.records()))
        self.assertNotIn('private-source-marker',dumps(self.store.records()))

    def test_invalid_or_partial_pagination_cannot_authorize_post_or_adopt(self):
        pagesets=[[{'items':[],'has_more':True,'page_token':''}],
                  [{'items':[reaction()],'has_more':True,'page_token':'0'}],
                  [{'items':None,'has_more':False}]]
        for index, pages in enumerate(pagesets):
            self.cli.pages=pages;self.assertEqual(self.add('bad-page-'+str(index))['status'],'failed')
        self.assertEqual(self.cli.posts(),[])

    def test_interruption_and_stale_sending_recovery_never_post_again(self):
        self.cli.post_error='interrupt'
        with self.assertRaises(KeyboardInterrupt):self.add()
        record=self.store.records(reactions.KIND)[0];data=dict(record['data'],status='sending')
        self.store.update_record(record['id'],data)
        count=len(self.cli.calls);result=self.add()
        self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(len(self.cli.calls),count)
        self.assertEqual(self.inspect(result)['status'],'completed');self.assertEqual(len(self.cli.posts()),1)

    def test_request_identity_binding_and_remote_source_lock(self):
        result=self.add()
        with self.assertRaises(reactions.ReactionError):self.add(emoji='OK')
        changed=dict(self.settings,expected_app_id='cli_other')
        with self.assertRaises(reactions.ReactionError):reactions.inspect(self.store,changed,result['action_id'],process=self.cli)
        with self.store.inspection_lock(['reaction-source:'+result['target_hash']]):
            with self.assertRaises(Busy):self.add('concurrent-source')
        self.assertEqual(len(self.cli.posts()),1)

    def test_dryrun_and_invalid_config_do_not_launch_cli(self):
        result=reactions.add(self.store,self.settings,'preview','oc_fixture','om_source',process=self.cli)
        self.assertEqual(result['status'],'dry_run');self.assertEqual(self.cli.calls,[])
        self.assertEqual(self.store.records(reactions.KIND),[])
        for emoji in ('SMILE','get','',None,[]):
            with self.assertRaises(ValueError):self.add(emoji=emoji)
        args=reactions.parser().parse_args(['add','--request-id','fixture','--chat-id','oc_fixture','--message-id','om_source'])
        self.assertEqual(args.emoji,'Get');self.assertFalse(args.confirm)


class ReactionInboundTests(unittest.TestCase):
    def setUp(self):
        test_inbound.InboundTests.setUp(self)
        self.conf['inbound']['group_ack_policy']='reaction';self.reaction_calls=[];self.reaction_status='completed';self.reaction_error=None
        def reactor(request,chat,message,emoji):
            record=self.store.get(request.split(':')[1])
            self.assertEqual(record['data']['ack_reaction_intent']['request_id'],request)
            self.reaction_calls.append((request,chat,message,emoji,len(self.ds.calls)))
            if self.reaction_error:raise self.reaction_error
            return {'status':self.reaction_status,'verification_status':'verified' if self.reaction_status=='completed' else 'unresolved'}
        self.reactor=reactor;self.engine=self.new_engine()

    def new_engine(self):
        return inbound.Engine(self.store,self.conf,self.ds,self.paseo,self.worktrees,self.sender,'ou_bot',reactor=getattr(self,'reactor',lambda *_:None))
    accept=test_inbound.InboundTests.accept
    deep=test_inbound.InboundTests.deep

    def test_group_reaction_precedes_ds_quick_final_once_and_model_cannot_select_target(self):
        record=self.accept(chat_type='group',mentions=[{'id':'ou_bot'}],emoji_type='SMILE',reply_to='om_other')
        done=self.engine.advance(record);self.assertEqual(done['data']['status'],'completed')
        self.assertEqual(self.reaction_calls,[('inbound:'+record['id']+':ack-reaction','oc_fixture','om_synthetic','Get',0)])
        self.assertEqual(len(self.replies),1);self.assertFalse(done['data']['final_delivery']['reply_in_thread'])
        self.engine.advance(done);self.assertEqual(len(self.reaction_calls),1)

    def test_default_shared_reactor_journals_real_offline_cli_flow_before_quick_answer(self):
        cli=FakeCLI(self)
        self.conf['publisher'].update(cli_argv=['synthetic-lark'],cli_profile='fixture',cli_config_dir=str(self.root))
        self.engine=self.new_engine();self.engine.reactor=self.engine._react
        original=reactions.add
        def shared_add(*args,**kwargs):
            self.assertEqual(len(self.ds.calls),0)
            self.assertEqual(kwargs['budget_s'],10)
            return original(*args,process=cli,**kwargs)
        with patch('work_assistant.inbound.reactions.add',side_effect=shared_add),patch('work_assistant.lark_cli.time.sleep'):
            record=self.accept(message_id='om_source',chat_type='group',mentions=[{'id':'ou_bot'}])
            done=self.engine.advance(record);self.engine.advance(done)
        self.assertEqual(done['data']['status'],'completed');self.assertEqual(len(cli.posts()),1)
        self.assertEqual(len(self.replies),1);self.assertEqual(len(self.ds.calls),1)
        receipt=done['data']['ack_reaction_receipt']
        self.assertEqual(receipt['status'],'completed');self.assertEqual(receipt['verification_status'],'verified')
        self.assertEqual(self.store.get(receipt['action_id'])['kind'],reactions.KIND)

    def test_default_reaction_budget_exhaustion_continues_ds_and_final_exactly_once(self):
        cli=FakeCLI(self);timed=TimedCLI(cli,[1,2,3,5])
        self.conf['publisher'].update(cli_argv=['synthetic-lark'],cli_profile='fixture',cli_config_dir=str(self.root))
        self.engine=self.new_engine();self.engine.reactor=self.engine._react
        original=reactions.add
        def shared_add(*args,**kwargs):
            self.assertEqual(len(self.ds.calls),0);self.assertEqual(kwargs['budget_s'],10)
            return original(*args,process=timed,clock=timed.clock,**kwargs)
        with patch('work_assistant.inbound.reactions.add',side_effect=shared_add),patch('work_assistant.lark_cli.time.sleep'):
            record=self.accept(message_id='om_source',chat_type='group',mentions=[{'id':'ou_bot'}])
            done=self.engine.advance(record);self.engine.advance(done)
        self.assertEqual(done['data']['status'],'completed');self.assertEqual(len(cli.posts()),0)
        self.assertEqual(len(self.ds.calls),1);self.assertEqual(len(self.replies),1)
        self.assertEqual(done['data']['ack_reaction_receipt']['diagnostic']['phase'],'list')
        self.assertEqual(done['data']['ack_reaction_receipt']['diagnostic']['elapsed_ms'],10000)
        self.assertFalse(done['data']['final_delivery']['reply_in_thread'])

    def test_escalation_has_reaction_and_no_text_ack_then_final_despite_failed_or_unknown_reaction(self):
        for index, status in enumerate(('failed','outcome_unknown')):
            self.reaction_status=status;self.paseo.matches=[]
            record=self.deep(message_id='om_escalation_'+str(index),chat_type='group',mentions=[{'id':'ou_bot'}])
            self.assertEqual(record['data']['status'],'gpt_running');self.assertNotIn('ack_intent',record['data'])
            self.assertEqual(record['data']['ack_reaction_receipt']['status'],status)
            self.assertEqual(self.engine.advance(record)['data']['status'],'completed')
        self.assertEqual(len(self.replies),2);self.assertEqual(len(self.reaction_calls),2)

    def test_reaction_exception_does_not_replace_answer_or_block_ds(self):
        self.reaction_error=ValueError('synthetic failure')
        done=self.engine.advance(self.accept(chat_type='group',mentions=[{'id':'ou_bot'}]))
        self.assertEqual(done['data']['status'],'completed');self.assertEqual(len(self.ds.calls),1)
        self.assertEqual(done['data']['ack_reaction_receipt']['status'],'outcome_unknown');self.assertEqual(len(self.replies),1)

    def test_crash_after_reaction_intent_recovers_without_reactor_replay(self):
        self.reaction_error=KeyboardInterrupt();record=self.accept(chat_type='group',mentions=[{'id':'ou_bot'}])
        with self.assertRaises(KeyboardInterrupt):self.engine.advance(record)
        self.reaction_error=None;self.engine=self.new_engine()
        done=self.engine.advance(self.store.get(record['id']))
        self.assertEqual(done['data']['status'],'completed');self.assertEqual(len(self.reaction_calls),1)

    def test_old_received_event_and_frozen_final_are_not_backfilled(self):
        record=self.accept(chat_type='group',mentions=[{'id':'ou_bot'}]);data=dict(record['data']);data.pop('ack_eligible')
        self.store.update_record(record['id'],data)
        done=self.engine.advance(self.store.get(record['id']));self.assertEqual(done['data']['status'],'completed')
        self.assertEqual(self.reaction_calls,[])
        record=self.accept(message_id='om_old_plan',chat_type='group',mentions=[{'id':'ou_bot'}])
        path=self.store.root/'inbound'/record['id']/'final.txt';test_inbound.private_write(path,b'Legacy frozen text')
        record=self.engine.save(record,status='finalizing',completion_status='completed',final_text_hash=digest(b'Legacy frozen text'))
        done=self.engine.advance(record);self.assertEqual(done['data']['final_intent']['delivery'],{})
        self.assertEqual(self.reaction_calls,[])

    def test_private_chat_keeps_text_ack_and_invalid_emoji_is_rejected(self):
        record=self.deep();self.assertIn('ack_intent',record['data']);self.assertEqual(self.reaction_calls,[])
        self.assertEqual(len(self.replies),1)
        for value in ('get','SMILE',None,[]):
            self.conf['inbound']['ack_reaction']=value
            with self.assertRaisesRegex(ValueError,'ack_reaction'):self.new_engine()
