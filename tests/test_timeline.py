"""Fixed-document contracts over a synthetic HTTP server; no live provider calls."""
import copy
import json
import os
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from work_assistant.cli import parser
from work_assistant.config import load
from work_assistant.feishu import publish, reconcile, http, AmbiguousError
from work_assistant.timeline import adopt_timeline, rename_timeline, binding_key, root_ids
from work_assistant.reports import generate_report
from work_assistant.store import Busy
from work_assistant.util import dumps, digest
from test_feishu_native import NativeServer, block
from test_runtime import Base


class TimelineTests(Base):
    def setUp(self):
        super().setUp()
        self.server=NativeServer(multiple_messages=True);self.addCleanup(self.server.close)
        env=patch.dict(os.environ,{'FEISHU_APP_ID':'synthetic-app','FEISHU_APP_SECRET':'synthetic-secret','FEISHU_RECEIVE_ID':'synthetic-self'})
        env.start();self.addCleanup(env.stop)
        self.settings=dict(self.conf['publisher'],document_layout='timeline',timeline_title='Project Daily Sync',timeline_max_blocks=100)
        self.task();self.template=generate_report(self.s,self.p['id'],'2026-01-15')

    def report(self,day='2026-01-15',children=None,card='Synthetic summary'):
        path=self.root/('timeline-report-'+str(len(self.s.records('report'))));path.mkdir()
        content={name:(Path(self.template['data']['path'])/name).read_text() for name in self.template['data']['files']}
        document={'title':'Daily '+day,'children':children or [block(4,'Heading '+day),block(12,'Body '+day,True)]}
        content['document.json']=dumps(document)
        content['card.json']=dumps({'config':{'update_multi':True},'header':{'title':{'tag':'plain_text','content':'Daily '+day}},
            'elements':[{'tag':'div','text':{'tag':'plain_text','content':card}},{'tag':'note','elements':[]}]})
        for name,text in content.items():(path/name).write_text(text)
        return self.s.record('report',dict(self.template['data'],period=day,path=str(path),hash=digest(content),
            files={n:digest(t.encode()) for n,t in content.items()}),project_id=self.p['id'])

    def send(self,report,**kwargs):
        return publish(self.s,self.settings,report['id'],confirm=True,base=self.server.base,**kwargs)

    def binding(self):
        return self.s.records('doc_binding')[0]

    def writes(self,start=0):
        return [c for c in self.server.calls[start:] if c[0] in {'POST','PATCH','DELETE'} and '/tenant_access_token/' not in c[1]]

    def test_cross_day_same_doc_newest_first_history_ids_untouched_and_noop(self):
        day1=self.report('2026-01-14');first=self.send(day1);self.assertEqual(first['status'],'completed')
        old_ids=list(root_ids(self.binding()['data']))
        # Old-day human edits/comments must remain untouched by a new-day insert.
        self.server.blocks[1]['heading2']['elements'][0]['text_run']['content']='User-edited old-day text'
        self.server.blocks[1]['comment_ids']=['old-comment'];self.server.revision+=1
        day2=self.report('2026-01-15');before=len(self.server.calls);second=self.send(day2)
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['document_id'],first['document_id']);self.assertNotEqual(second['message_id'],first['message_id'])
        binding=self.binding()['data'];self.assertEqual(binding['order'],['2026-01-15','2026-01-14'])
        self.assertEqual(binding['sections']['2026-01-14']['block_ids'],old_ids)
        self.assertEqual([b['block_id'] for b in self.server.blocks],root_ids(binding))
        self.assertEqual(self.server.blocks[4]['heading2']['elements'][0]['text_run']['content'],'User-edited old-day text')
        self.assertFalse(any(c[0] in {'PATCH','DELETE'} for c in self.writes(before)))
        self.assertTrue(all(c[2]['index']==0 for c in self.writes(before) if c[1].endswith('/children')))
        count=len(self.server.calls);self.assertTrue(self.send(day2)['no_op']);self.assertEqual(len(self.server.calls),count)
        self.assertEqual(first['block_ids'],[]);self.assertEqual(second['block_ids'],[])
        self.assertEqual(sum(c[0]=='POST' and c[1]=='/docx/v1/documents' for c in self.server.calls),1)

    def test_same_day_changes_only_one_block_and_same_card(self):
        first=self.send(self.report());old=root_ids(self.binding()['data'])
        updated=self.report(children=[block(4,'Heading 2026-01-15'),block(12,'Changed detail',True)],card='Changed summary')
        before=len(self.server.calls);second=self.send(updated)
        self.assertEqual(second['status'],'completed');self.assertEqual(second['message_id'],first['message_id'])
        self.assertEqual(root_ids(self.binding()['data']),old)
        writes=self.writes(before)
        self.assertEqual([(c[0],c[1]) for c in writes],[('PATCH','/docx/v1/documents/doc-native/blocks/'+old[2]),('PATCH','/im/v1/messages/'+first['message_id'])])

    def test_non_top_structural_replacement_only_deletes_target_range(self):
        for day in ('2026-01-11','2026-01-13','2026-01-15'):self.send(self.report(day))
        before_binding=copy.deepcopy(self.binding()['data'])
        before=len(self.server.calls)
        update=self.report('2026-01-13',children=[block(5,'Changed type'),block(12,'Changed body'),block(2,'Extra paragraph')],card='Updated old-day summary')
        result=self.send(update);self.assertEqual(result['status'],'completed')
        after=self.binding()['data']
        for day in ('2026-01-11','2026-01-15'):
            self.assertEqual(after['sections'][day],before_binding['sections'][day])
        self.assertEqual([b['block_id'] for b in self.server.blocks],root_ids(after))
        insertion=next(c for c in self.writes(before) if c[0]=='POST')
        deletion=next(c for c in self.writes(before) if c[0]=='DELETE')
        self.assertEqual(insertion[2]['index'],3)
        self.assertEqual(deletion[2],{'start_index':7,'end_index':10})
        protected=set(before_binding['sections']['2026-01-11']['block_ids']+before_binding['sections']['2026-01-15']['block_ids'])
        self.assertFalse(any(c[0]=='PATCH' and c[1].rsplit('/',1)[1] in protected for c in self.writes(before)))

    def test_structural_or_target_user_edit_is_needs_user_zero_writes(self):
        first=self.send(self.report());saved=copy.deepcopy(self.server.blocks)
        report=self.report(children=[block(4,'Changed'),block(12,'Body 2026-01-15',True)])
        self.server.blocks.insert(0,dict(block(2,'Unexpected'),block_id='user-added',parent_id='doc-native'));self.server.revision+=1
        before=len(self.server.calls);refused=self.send(report)
        self.assertEqual(refused['status'],'needs_user');self.assertEqual(self.writes(before),[])
        self.server.blocks=saved;self.server.revision+=1
        reconcile(self.s,self.settings,refused['binding_record_id'],'retry_authorized','operator removed unexpected structure',base=self.server.base)
        self.server.blocks[1]['heading2']['elements'][0]['text_run']['content']='User changed target'
        before=len(self.server.calls);refused=self.send(report,retry=True)
        self.assertEqual(refused['status'],'needs_user');self.assertEqual(self.writes(before),[])
        self.assertEqual(refused['message_id'],first['message_id'])
        self.server.blocks=copy.deepcopy(saved);self.server.revision+=1
        reconcile(self.s,self.settings,refused['binding_record_id'],'retry_authorized','operator restored target text',base=self.server.base)
        self.server.blocks[1]['comment_ids']=['current-day-comment'];self.server.revision+=1
        before=len(self.server.calls);refused=self.send(report,retry=True)
        self.assertEqual(refused['status'],'needs_user');self.assertEqual(self.writes(before),[])

    def test_insert_unknown_applied_requires_reconcile_and_never_duplicates(self):
        self.send(self.report('2026-01-14'));report=self.report()
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='POST' and '/children?' in url:raise AmbiguousError('synthetic insert response lost')
            return result
        unknown=self.send(report,transport=lost);self.assertEqual(unknown['status'],'outcome_unknown')
        ids=[b['block_id'] for b in self.server.blocks];before=len(self.server.calls)
        with self.assertRaises(ValueError):self.send(report,retry=True)
        self.assertEqual(len(self.server.calls),before)
        readonly=reconcile(self.s,self.settings,unknown['binding_record_id'],base=self.server.base)
        self.assertEqual(readonly['inspection']['section']['phase'],'complete')
        self.assertEqual(self.binding()['data']['status'],'outcome_unknown')
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified inserted date section',base=self.server.base)
        before=len(self.server.calls);done=self.send(report,retry=True)
        self.assertEqual(done['status'],'completed');self.assertEqual([b['block_id'] for b in self.server.blocks],ids)
        self.assertFalse(any(c[1].startswith('/docx/') for c in self.writes(before)))

    def test_replacement_delete_unknown_only_commits_after_explicit_inspection(self):
        self.send(self.report());report=self.report(children=[block(5,'New type'),block(2,'Replacement')],card='New summary')
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='DELETE':raise AmbiguousError('synthetic delete acknowledgement lost')
            return result
        unknown=self.send(report,transport=lost);self.assertEqual(unknown['status'],'outcome_unknown')
        actual=[b['block_id'] for b in self.server.blocks]
        reconcile(self.s,self.settings,unknown['publication_id'],'retry_authorized','operator verified old range removed and exact new IDs',base=self.server.base)
        before=len(self.server.calls);done=self.send(report,retry=True)
        self.assertEqual(done['status'],'completed');self.assertEqual([b['block_id'] for b in self.server.blocks],actual)
        self.assertFalse(any(c[1].startswith('/docx/') for c in self.writes(before)))

    def test_shared_document_lock_and_binding_reload_inside_claim(self):
        destination='app:'+digest(['synthetic-app','chat_id','synthetic-self',''])
        key=binding_key(destination,self.p['id']);report=self.report()
        with self.s.claim(['doc:'+key]):
            with self.assertRaises(Busy):self.send(report)
        self.assertEqual(self.server.calls,[])
        original_claim=self.s.claim;inject=[True]
        newer=self.report('2026-01-16')
        @contextmanager
        def claim(resources):
            if inject[0]:
                inject[0]=False;self.send(newer)
            with original_claim(resources) as value:yield value
        with patch.object(self.s,'claim',claim):
            result=self.send(report)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.binding()['data']['order'],['2026-01-16','2026-01-15'])
        self.assertEqual(sum(c[0]=='POST' and c[1]=='/docx/v1/documents' for c in self.server.calls),1)

    def test_explicit_adoption_and_title_change_reuse_completed_doc_and_card(self):
        report=self.report();per=dict(self.settings,document_layout='per_period')
        first=publish(self.s,per,report['id'],confirm=True,base=self.server.base)
        before=len(self.server.calls)
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],base=self.server.base)
        self.assertEqual(len(self.server.calls),before)
        adopted=adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,title='Project Daily Sync',base=self.server.base)
        self.assertEqual(adopted['status'],'completed');self.assertTrue(adopted['renamed'])
        self.assertEqual(adopted['document_id'],first['document_id']);self.assertEqual(adopted['message_id'],first['message_id'])
        self.assertEqual(self.server.title,'Project Daily Sync')
        self.assertFalse(any(c[0]=='POST' for c in self.writes(before)))
        migrated=self.send(report);self.assertEqual(migrated['status'],'completed')
        self.assertEqual(migrated['message_id'],first['message_id'])
        self.assertEqual(self.server.blocks[0]['block_type'],3)
        self.assertEqual(len(self.server.blocks),3)
        with self.assertRaises(ValueError):publish(self.s,per,report['id'],confirm=True,base=self.server.base)

    def test_title_unknown_applied_then_explicit_reconcile_never_renames_again(self):
        report=self.report();first=publish(self.s,dict(self.settings,document_layout='per_period'),report['id'],confirm=True,base=self.server.base)
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='PATCH' and urllib.parse.urlsplit(url).path.endswith('/blocks/doc-native'):
                raise AmbiguousError('synthetic title response lost')
            return result
        unknown=adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,title='Project Daily Sync',transport=lost,base=self.server.base)
        self.assertEqual(unknown['status'],'outcome_unknown');self.assertEqual(self.server.title,'Project Daily Sync')
        with self.assertRaises(ValueError):self.send(report,retry=True)
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified requested title is present',base=self.server.base)
        before=len(self.server.calls);done=self.send(report)
        self.assertEqual(done['status'],'completed')
        self.assertFalse(any(c[0]=='PATCH' and c[1].endswith('/blocks/doc-native') for c in self.writes(before)))

    def test_explicit_rename_is_independent_of_per_day_card_titles(self):
        self.send(self.report());binding=self.binding()
        before=len(self.server.calls)
        renamed=rename_timeline(self.s,self.settings,binding['id'],'Canonical title',confirm=True,base=self.server.base)
        self.assertEqual(renamed['title'],'Canonical title')
        self.assertEqual([(c[0],c[1]) for c in self.writes(before)],[('PATCH','/docx/v1/documents/doc-native/blocks/doc-native')])
        self.settings['timeline_title']='Configuration does not silently rename'
        result=self.send(self.report('2026-01-16'))
        self.assertEqual(result['status'],'completed');self.assertEqual(self.server.title,'Canonical title')
        self.assertEqual(self.server.messages[result['message_id']]['header']['title']['content'],'Daily 2026-01-16')

    def test_local_limit_and_config_cli_contract(self):
        self.settings['timeline_max_blocks']=2
        refused=self.send(self.report());self.assertEqual(refused['status'],'needs_user');self.assertEqual(self.server.calls,[])
        self.assertEqual(self.binding()['data']['sections'],{})
        path=self.root/'timeline-config.json'
        for changes in ({'document_layout':'bad'},{'document_layout':'timeline','mode':'webhook'},{'timeline_max_blocks':True},{'timeline_title':''}):
            path.write_text(dumps({'publisher':changes}))
            with self.assertRaises(ValueError):load(path)
        args=parser().parse_args(['publish','adopt-timeline','publication_fixture','--confirm','--title','Project Daily Sync'])
        self.assertEqual(args.id,'adopt-timeline');self.assertEqual(args.target_id,'publication_fixture');self.assertTrue(args.confirm)
        args=parser().parse_args(['publish','rename-timeline','doc_binding_fixture','--confirm','--title','Renamed'])
        self.assertEqual(args.id,'rename-timeline')

    def test_card_only_revision_checks_structure_before_card_patch(self):
        first=self.send(self.report());update=self.report(card='Approved card-only revision')
        before=len(self.server.calls);done=self.send(update)
        self.assertEqual(done['status'],'completed');self.assertEqual(done['message_id'],first['message_id'])
        self.assertEqual([(c[0],c[1]) for c in self.writes(before)],[('PATCH','/im/v1/messages/'+first['message_id'])])
        self.assertEqual(self.binding()['data']['sections']['2026-01-15']['report_id'],update['id'])
        self.server.blocks.append(dict(block(2,'External structure'),block_id='external',parent_id='doc-native'));self.server.revision+=1
        before=len(self.server.calls);refused=self.send(self.report(card='Another card revision'))
        self.assertEqual(refused['status'],'needs_user');self.assertEqual(self.writes(before),[])

    def test_unknown_patch_applied_is_not_repeated_after_resolution(self):
        self.send(self.report());update=self.report(children=[block(4,'Changed heading'),block(12,'Body 2026-01-15',True)],card='Changed summary')
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='PATCH' and '/docx/' in url:raise AmbiguousError('synthetic patch response lost')
            return result
        unknown=self.send(update,transport=lost);self.assertEqual(unknown['status'],'outcome_unknown')
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified intended changed block',base=self.server.base)
        before=len(self.server.calls);done=self.send(update,retry=True)
        self.assertEqual(done['status'],'completed')
        self.assertFalse(any(c[1].startswith('/docx/') for c in self.writes(before)))

    def test_unknown_title_absent_needs_explicit_rename_retry(self):
        self.send(self.report());binding=self.binding()
        def absent(method,url,body,headers):
            if method=='PATCH' and urllib.parse.urlsplit(url).path.endswith('/blocks/doc-native'):
                raise AmbiguousError('synthetic disconnected title request')
            return http(method,url,body,headers)
        unknown=rename_timeline(self.s,self.settings,binding['id'],'New canonical',confirm=True,transport=absent,base=self.server.base)
        self.assertEqual(unknown['status'],'outcome_unknown')
        with self.assertRaises(ValueError):rename_timeline(self.s,self.settings,binding['id'],'New canonical',confirm=True,retry=True,base=self.server.base)
        reconcile(self.s,self.settings,binding['id'],'retry_authorized','operator verified old title remains',base=self.server.base)
        with self.assertRaises(ValueError):self.send(self.report('2026-01-16'),retry=True)
        done=rename_timeline(self.s,self.settings,binding['id'],'New canonical',confirm=True,retry=True,base=self.server.base)
        self.assertEqual(done['status'],'completed');self.assertEqual(self.server.title,'New canonical')
        self.assertEqual(sum(c[0]=='PATCH' and c[1].endswith('/blocks/doc-native') for c in self.server.calls),1)

    def test_adoption_rejects_user_edits_and_unresolved_publications(self):
        report=self.report();first=publish(self.s,dict(self.settings,document_layout='per_period'),report['id'],confirm=True,base=self.server.base)
        self.server.blocks[0]['heading2']['elements'][0]['text_run']['content']='User change';self.server.revision+=1
        before=len(self.server.calls)
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,base=self.server.base)
        self.assertEqual(self.writes(before),[]);self.assertEqual(self.s.records('doc_binding'),[])
        data=self.s.get(first['publication_id'])['data'];data['status']='outcome_unknown';self.s.update_record(first['publication_id'],data)
        before=len(self.server.calls)
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,base=self.server.base)
        self.assertEqual(len(self.server.calls),before)

    def test_unknown_created_doc_is_explicitly_adopted_without_second_create(self):
        report=self.report()
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='POST' and urllib.parse.urlsplit(url).path=='/docx/v1/documents':raise AmbiguousError('synthetic created doc response lost')
            return result
        unknown=self.send(report,transport=lost);self.assertEqual(unknown['status'],'outcome_unknown');self.assertIsNone(unknown['document_id'])
        with self.assertRaises(ValueError):reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','no document ID supplied',base=self.server.base)
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator found exact newly created empty document',{'document_id':'doc-native'},base=self.server.base)
        done=self.send(report,retry=True);self.assertEqual(done['status'],'completed')
        self.assertEqual(sum(c[0]=='POST' and c[1]=='/docx/v1/documents' for c in self.server.calls),1)

    def test_unknown_card_does_not_freeze_shared_doc_or_invent_card_identity(self):
        report=self.report('2026-01-14')
        def lost(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='POST' and urllib.parse.urlsplit(url).path=='/im/v1/messages':raise AmbiguousError('synthetic card send response lost')
            return result
        unknown=self.send(report,transport=lost);self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(self.binding()['data']['status'],'completed')
        with self.assertRaises(ValueError):self.send(report,retry=True)
        self.assertEqual(self.send(self.report('2026-01-15'))['status'],'completed')
        with self.assertRaises(ValueError):reconcile(self.s,self.settings,unknown['publication_id'],'retry_authorized','unknown card not inspected',base=self.server.base)
        self.assertEqual(self.binding()['data']['status'],'completed')
        reconcile(self.s,self.settings,unknown['publication_id'],'completed','operator verified the exact sent daily card',{'message_id':'message-native-1'},base=self.server.base)
        self.assertEqual(self.binding()['data']['status'],'completed')
        before=len(self.server.calls);self.assertTrue(self.send(report)['no_op']);self.assertEqual(len(self.server.calls),before)
        self.assertEqual(len(self.server.messages),2)

    def test_missing_insert_response_ids_are_not_replayed(self):
        report=self.report()
        def missing_ids(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='POST' and '/children?' in url:result['data']['children']=[]
            return result
        unknown=self.send(report,transport=missing_ids)
        self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(self.binding()['data']['operation']['new_ids'],[])
        actual=[b['block_id'] for b in self.server.blocks]
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified all inserted IDs despite missing response IDs',base=self.server.base)
        before=len(self.server.calls);done=self.send(report,retry=True)
        self.assertEqual(done['status'],'completed');self.assertEqual([b['block_id'] for b in self.server.blocks],actual)
        self.assertFalse(any(c[1].startswith('/docx/') for c in self.writes(before)))

    def test_unknown_delete_not_applied_retries_only_verified_old_range(self):
        self.send(self.report());update=self.report(children=[block(5,'Changed type'),block(2,'Changed details')],card='Changed')
        def disconnected(method,url,body,headers):
            if method=='DELETE':raise AmbiguousError('synthetic delete not received')
            return http(method,url,body,headers)
        unknown=self.send(update,transport=disconnected);self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(len(self.server.blocks),6)
        view=reconcile(self.s,self.settings,unknown['binding_record_id'],base=self.server.base)
        self.assertEqual(view['inspection']['section']['phase'],'ready_delete')
        with self.assertRaises(ValueError):reconcile(self.s,self.settings,unknown['binding_record_id'],'completed','old section still exists',base=self.server.base)
        reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified inserted section and untouched old range',base=self.server.base)
        before=len(self.server.calls);done=self.send(update,retry=True)
        self.assertEqual(done['status'],'completed');self.assertEqual(len(self.server.blocks),3)
        self.assertEqual(sum(c[0]=='DELETE' for c in self.writes(before)),1)
        self.assertFalse(any(c[0]=='POST' and c[1].endswith('/children') for c in self.writes(before)))

    def test_unknown_member_grant_requires_confirmation_and_is_not_duplicated(self):
        self.settings['document_member_env']='SYNTHETIC_MEMBER'
        with patch.dict(os.environ,{'SYNTHETIC_MEMBER':'ou_synthetic'}):
            def lost(method,url,body,headers):
                result=http(method,url,body,headers)
                if method=='POST' and '/drive/v1/permissions/' in url:raise AmbiguousError('synthetic share response lost')
                return result
            report=self.report();unknown=self.send(report,transport=lost)
            self.assertEqual(unknown['status'],'outcome_unknown')
            with self.assertRaises(ValueError):reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','share not confirmed',base=self.server.base)
            reconcile(self.s,self.settings,unknown['binding_record_id'],'retry_authorized','operator verified own member grant',{'document_member_granted':True},base=self.server.base)
            self.assertEqual(self.send(report,retry=True)['status'],'completed')
            self.assertEqual(sum(c[0]=='POST' and '/drive/v1/permissions/' in c[1] for c in self.server.calls),1)

    def test_multi_batch_date_insertion_uses_bounded_offsets_and_keeps_history(self):
        self.send(self.report('2026-01-14'));old=list(root_ids(self.binding()['data']))
        report=self.report(children=[block(12,'Synthetic list %s'%i) for i in range(55)])
        before=len(self.server.calls);done=self.send(report)
        self.assertEqual(done['status'],'completed')
        batches=[c for c in self.writes(before) if c[0]=='POST' and c[1].endswith('/children')]
        self.assertEqual([(c[2]['index'],len(c[2]['children'])) for c in batches],[(0,50),(50,6)])
        self.assertEqual(self.binding()['data']['sections']['2026-01-14']['block_ids'],old)
        self.assertEqual([b['block_id'] for b in self.server.blocks][-3:],old)
        self.assertEqual(len(self.server.blocks),59)
