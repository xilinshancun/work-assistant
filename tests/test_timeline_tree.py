"""Native tree timeline tests over an isolated synthetic HTTP document service."""
import copy
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from work_assistant.feishu import publish, reconcile, http, AmbiguousError, preview, TEXT_BLOCKS
from work_assistant.timeline import adopt_timeline, rename_timeline, publish_timeline
from work_assistant import document_tree as tree
from work_assistant.reports import generate_report
from work_assistant.util import dumps, digest
from test_feishu_native import block
from test_runtime import Base


def callout(color, text='Synthetic detail'):
    return {'block_type':19,'callout':{'background_color':color,'border_color':color,'emoji_id':'writing_hand' if color==2 else 'bulb'},
            'child_blocks':[block(4,'Deliverables' if color==2 else 'Sync'),block(12,text,True)]}


class TreeServer:
    def __init__(self):
        self.calls=[];self.nodes={};self.title='';self.revision=1;self.counter=0;self.messages={}
        self.root='doc-tree';self.after_write=None
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def handle_request(self):
                p=urllib.parse.urlsplit(self.path);path=p.path;q=dict(urllib.parse.parse_qsl(p.query))
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')));body=json.loads(raw) if raw else None
                outer.calls.append((self.command,path,body,q));data={}
                if path.endswith('/tenant_access_token/internal'):
                    response={'code':0,'tenant_access_token':'synthetic-token'}
                else:
                    if path=='/docx/v1/documents' and self.command=='POST':
                        outer.title=body['title'];outer.nodes[outer.root]={'block_id':outer.root,'parent_id':'','block_type':1,'children':[],
                            'page':{'elements':[{'text_run':{'content':outer.title}}]}}
                        data={'document':{'document_id':outer.root}}
                    elif path=='/docx/v1/documents/'+outer.root:
                        data={'document':outer.metadata()}
                    elif path.endswith('/blocks') and self.command=='GET':
                        start=int(q.get('page_token','0'));items=list(outer.nodes.values());page=copy.deepcopy(items[start:start+5])
                        for b in page:
                            if b['block_type'] in TEXT_BLOCKS:
                                native=b[TEXT_BLOCKS[b['block_type']]];native.setdefault('style',{}).update(align=1,folded=False)
                                for e in native['elements']:
                                    s=e['text_run'].setdefault('text_element_style',{})
                                    for k in ('bold','italic','underline','strikethrough','inline_code'):s.setdefault(k,False)
                            elif b['block_type']==19:b['callout'].setdefault('text_color',0)
                        data={'items':page,'has_more':start+5<len(items),'page_token':str(start+5)}
                    elif '/blocks/' in path:
                        tail=path.split('/blocks/',1)[1];bid=tail.split('/')[0]
                        if self.command=='GET' and tail.endswith('/children'):
                            ids=outer.nodes[bid]['children'];start=int(q.get('page_token','0'))
                            data={'items':[copy.deepcopy(outer.nodes[i]) for i in ids[start:start+5]],'has_more':start+5<len(ids),'page_token':str(start+5)}
                        elif self.command=='GET':data={'block':copy.deepcopy(outer.nodes[bid])}
                        elif self.command=='PATCH':
                            key='page' if bid==outer.root else TEXT_BLOCKS[outer.nodes[bid]['block_type']]
                            outer.nodes[bid][key]['elements']=copy.deepcopy(body['update_text_elements']['elements'])
                            if bid==outer.root:outer.title=''.join(e['text_run']['content'] for e in body['update_text_elements']['elements'])
                            outer.revision+=1;data={'block':copy.deepcopy(outer.nodes[bid]),'document_revision_id':outer.revision}
                        elif tail.endswith('/descendant'):
                            assert len(body['children_id'])<=50 and len(body['descendants'])<=50
                            assert len({b['block_id'] for b in body['descendants']})==len(body['descendants'])
                            mapping={b['block_id']:outer.ident() for b in body['descendants']};parent={}
                            for b in body['descendants']:
                                for child in b.get('children',[]):parent[child]=b['block_id']
                            for b in body['descendants']:
                                clone=copy.deepcopy(b);temporary=b['block_id'];clone.update(block_id=mapping[temporary],parent_id=mapping[parent[temporary]] if temporary in parent else bid)
                                if 'children' in clone:clone['children']=[mapping[c] for c in clone['children']]
                                if clone['block_type']==19:assert clone.get('children')
                                outer.nodes[clone['block_id']]=clone
                            roots=[mapping[i] for i in body['children_id']];outer.insert(bid,body['index'],roots);outer.revision+=1
                            data={'children':[copy.deepcopy(outer.nodes[i]) for i in roots],'document_revision_id':outer.revision,
                                  'block_id_relations':[{'temporary_block_id':a,'block_id':b} for a,b in mapping.items()]}
                        elif tail.endswith('/children/batch_delete'):
                            ids=outer.nodes[bid]['children'];removed=ids[body['start_index']:body['end_index']];del ids[body['start_index']:body['end_index']]
                            for i in removed:outer.remove(i)
                            outer.revision+=1;data={'document_revision_id':outer.revision}
                        elif tail.endswith('/children'):
                            assert 1<=len(body['children'])<=50
                            created=[outer.add(b,bid) for b in body['children']];outer.insert(bid,body['index'],created);outer.revision+=1
                            data={'children':[copy.deepcopy(outer.nodes[i]) for i in created],'document_revision_id':outer.revision}
                        else:raise AssertionError('unexpected synthetic block route')
                    elif path=='/im/v1/messages' and self.command=='POST':
                        mid='synthetic-message-%s'%(len(outer.messages)+1);outer.messages[mid]=json.loads(body['content']);data={'message_id':mid}
                    elif path.startswith('/im/v1/messages/'):
                        mid=path.rsplit('/',1)[1]
                        if self.command=='PATCH':outer.messages[mid]=json.loads(body['content'])
                        else:data={'items':[{'message_id':mid,'body':{'content':dumps(outer.messages[mid])}}]}
                    elif path.startswith('/drive/'):
                        data={}
                    else:raise AssertionError('unexpected synthetic API route: '+path)
                    if self.command in {'POST','PATCH','DELETE'} and path.startswith('/docx/') and outer.after_write:
                        outer.after_write(outer,path,body)
                    response={'code':0,'data':data}
                raw=dumps(response).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            do_GET=do_POST=do_PATCH=do_DELETE=handle_request
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True);self.thread.start()
        self.base='http://127.0.0.1:%s'%self.server.server_port

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def ident(self):self.counter+=1;return 'tree-%s'%self.counter
    def metadata(self):return {'document_id':self.root,'revision_id':self.revision,'title':self.title}
    def insert(self,parent,index,ids):
        values=self.nodes[parent].setdefault('children',[]);index=len(values) if index==-1 else index;values[index:index]=ids
    def add(self,native,parent):
        bid=self.ident();self.nodes[bid]=dict(copy.deepcopy(native),block_id=bid,parent_id=parent);return bid
    def remove(self,bid):
        for child in self.nodes[bid].get('children',[]):self.remove(child)
        del self.nodes[bid]
    def snapshot(self):return {'before':{'document':self.metadata()},'after':{'document':self.metadata()},'blocks':copy.deepcopy(list(self.nodes.values()))}


class TreeTimelineTests(Base):
    def setUp(self):
        super().setUp();self.server=TreeServer();self.addCleanup(self.server.close)
        env=patch.dict(os.environ,{'FEISHU_APP_ID':'synthetic-app','FEISHU_APP_SECRET':'synthetic-secret','FEISHU_RECEIVE_ID':'synthetic-self'})
        env.start();self.addCleanup(env.stop)
        self.settings=dict(self.conf['publisher'],document_layout='timeline',timeline_title='Synthetic Daily Sync',timeline_max_blocks=1000)
        self.task();self.template=generate_report(self.s,self.p['id'],'2026-01-15')

    def report(self,period='2026-01-15',blocks=None,tree_format=True,card='Synthetic card'):
        blocks=copy.deepcopy(blocks if blocks is not None else [block(2,'Coverage'),callout(2),callout(5),block(4,'Other section')])
        path=self.root/('tree-report-'+str(len(self.s.records('report'))));path.mkdir()
        contents={n:(Path(self.template['data']['path'])/n).read_text() for n in self.template['data']['files']}
        doc={'format':tree.FORMAT,'title':'Day '+period,'blocks':blocks} if tree_format else {'title':'Day '+period,'children':blocks}
        contents['document.json']=dumps(doc);contents['card.json']=dumps({'config':{'update_multi':True},'elements':[{'tag':'div','text':{'tag':'plain_text','content':card}}]})
        for name,text in contents.items():(path/name).write_text(text)
        return self.s.record('report',dict(self.template['data'],period=period,path=str(path),hash=digest(contents),files={n:digest(v.encode()) for n,v in contents.items()}),project_id=self.p['id'])

    def send(self,report,**kwargs):return publish(self.s,self.settings,report['id'],confirm=True,base=self.server.base,**kwargs)
    def document_only(self,report,**kwargs):
        return publish_timeline(self.s,self.settings,report['id'],confirm=True,base=self.server.base,document_only=True,**kwargs)
    def writes(self,start=0):return [c for c in self.server.calls[start:] if c[0] in {'POST','PATCH','DELETE'} and '/tenant_access_token/' not in c[1]]
    def binding(self):return self.s.records('doc_binding')[0]
    def assertCompleted(self,result):self.assertEqual(result['status'],'completed',result.get('error',result))

    def test_tree_create_update_same_leaf_and_day_card_then_zero_request_noop(self):
        original=self.report();self.assertEqual(preview(self.s,original['id'])['document']['format'],tree.FORMAT)
        first=self.send(original);self.assertCompleted(first);before_ids=set(self.server.nodes)
        self.assertEqual(self.server.nodes[self.server.nodes[self.server.root]['children'][0]]['block_type'],3)
        changed=[block(2,'Coverage'),callout(2,'Corrected detail'),callout(5),block(4,'Other section')]
        before=len(self.server.calls);second=self.send(self.report(blocks=changed,card='Updated summary'));self.assertCompleted(second)
        self.assertEqual(set(self.server.nodes),before_ids);self.assertEqual(second['message_id'],first['message_id'])
        self.assertEqual([c[0] for c in self.writes(before)],['PATCH','PATCH'])
        self.assertTrue(self.writes(before)[0][1].startswith('/docx/'))
        before=len(self.server.calls);self.assertTrue(self.send(self.s.get(second['report_id']))['no_op']);self.assertEqual(len(self.server.calls),before)

    def test_document_only_backfill_preserves_adopted_newest_tree_and_card(self):
        first,path,orange,blank=self.surgery('2026-01-29')
        adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),base=self.server.base)
        newest=copy.deepcopy(self.binding()['data']['sections']['2026-01-29'])
        preserved=copy.deepcopy(self.server.nodes);cards=copy.deepcopy(self.server.messages)
        original_publication=copy.deepcopy(self.s.get(first['publication_id']))
        before=len(self.server.calls)
        for day in ('2026-01-28','2026-01-27'):
            report=self.report(day)
            result=self.document_only(report);self.assertCompleted(result)
            self.assertEqual(result['document_status'],'synchronized')
            self.assertEqual(result['message_status'],'not_requested')
            self.assertIsNone(result['message_id'])
            count=len(self.server.calls)
            self.assertTrue(self.document_only(report)['no_op'])
            self.assertEqual(len(self.server.calls),count)
        binding=self.binding()['data']
        self.assertEqual(binding['order'],['2026-01-29','2026-01-28','2026-01-27'])
        self.assertEqual(self.server.nodes[self.server.root]['children'],[bid for day in binding['order'] for bid in binding['sections'][day]['block_ids']])
        self.assertEqual(binding['sections']['2026-01-29'],newest)
        for bid,b in preserved.items():
            if bid!=self.server.root:self.assertEqual(self.server.nodes[bid],b)
        self.assertEqual(self.server.nodes[orange]['children'][-1],blank)
        self.assertEqual(self.server.messages,cards)
        self.assertEqual(self.s.get(first['publication_id']),original_publication)
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))
        self.assertFalse(any(c[0] in {'PATCH','DELETE'} for c in self.writes(before)))

    def test_document_only_then_explicit_normal_publish_sends_card_without_document_rewrite(self):
        self.assertCompleted(self.send(self.report('2026-01-29')))
        history=self.report('2026-01-28')
        doc=self.document_only(history);self.assertCompleted(doc)
        before=len(self.server.calls);ids=copy.deepcopy(self.server.nodes)
        normal=self.send(history);self.assertCompleted(normal)
        self.assertTrue(normal['message_id']);self.assertNotEqual(normal['publication_id'],doc['publication_id'])
        self.assertEqual(self.server.nodes,ids)
        self.assertEqual([(c[0],c[1]) for c in self.writes(before)],[('POST','/im/v1/messages')])
        count=len(self.server.calls);self.assertTrue(self.send(history)['no_op']);self.assertEqual(len(self.server.calls),count)
        self.assertEqual(self.s.get(doc['publication_id'])['data']['message_status'],'not_requested')

    def test_document_only_revision_keeps_existing_card_hash_receipt_and_ordinary_checkpoint(self):
        first=self.send(self.report());self.assertCompleted(first)
        original=copy.deepcopy(self.s.get(first['publication_id']));cards=copy.deepcopy(self.server.messages)
        changed=self.report(blocks=[block(2,'Coverage'),callout(2,'Corrected history'),callout(5)],card='New summary not requested yet')
        before=len(self.server.calls);doc=self.document_only(changed);self.assertCompleted(doc)
        self.assertEqual(doc['message_id'],first['message_id'])
        self.assertEqual(doc['preserved_card_hash'],original['data']['hash'])
        self.assertEqual(doc['preserved_card_payload_hash'],original['data']['card_payload_hash'])
        self.assertEqual(self.s.get(first['publication_id']),original)
        self.assertEqual(self.server.messages,cards)
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))
        count=len(self.server.calls);self.assertTrue(self.document_only(changed)['no_op']);self.assertEqual(len(self.server.calls),count)
        before=len(self.server.calls);normal=self.send(changed);self.assertCompleted(normal)
        self.assertEqual(normal['message_id'],first['message_id'])
        self.assertEqual([(c[0],c[1]) for c in self.writes(before)],[('PATCH','/im/v1/messages/'+first['message_id'])])

    def test_document_only_unknown_insert_stops_both_scopes_and_reconciles_without_messages(self):
        self.assertCompleted(self.send(self.report('2026-01-29')))
        history=self.report('2026-01-28')
        result=self.document_only(history,transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/descendant')))
        self.assertEqual(result['status'],'outcome_unknown')
        self.assertEqual(result['document_status'],'outcome_unknown')
        ids=copy.deepcopy(self.server.nodes);before=len(self.server.calls)
        with self.assertRaises(ValueError):self.document_only(history,retry=True)
        with self.assertRaises(ValueError):self.send(history,retry=True)
        self.assertEqual(len(self.server.calls),before)
        inspected=reconcile(self.s,self.settings,result['publication_id'],base=self.server.base)
        self.assertEqual(inspected['inspection']['section']['phase'],'complete')
        self.assertEqual(self.writes(before),[])
        reconcile(self.s,self.settings,result['binding_record_id'],resolution='retry_authorized',reason='Operator inspected exact history tree',base=self.server.base)
        self.assertEqual(self.writes(before),[])
        done=self.document_only(history,retry=True);self.assertCompleted(done)
        self.assertEqual(done['message_status'],'not_requested')
        self.assertEqual(self.server.nodes,ids)
        self.assertEqual(self.writes(before),[])
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))

    def test_document_only_unknown_can_complete_via_publication_without_card_receipt(self):
        self.assertCompleted(self.send(self.report('2026-01-29')))
        history=self.report('2026-01-28',blocks=[callout(2)])
        result=self.document_only(history,transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/descendant')))
        self.assertEqual(result['status'],'outcome_unknown');before=len(self.server.calls)
        done=reconcile(self.s,self.settings,result['publication_id'],resolution='completed',reason='Operator checked exact complete history tree',base=self.server.base)
        self.assertEqual(done['publication']['status'],'completed')
        self.assertEqual(done['publication']['document_status'],'synchronized')
        self.assertIsNone(done['publication']['message_id'])
        self.assertEqual(self.writes(before),[])
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))
        count=len(self.server.calls);self.assertTrue(self.document_only(history)['no_op']);self.assertEqual(len(self.server.calls),count)

    def test_document_only_unknown_absent_insert_requires_explicit_original_scope_retry(self):
        self.assertCompleted(self.send(self.report('2026-01-29')))
        history=self.report('2026-01-28')
        result=self.document_only(history,transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/descendant'),after=False))
        self.assertEqual(result['status'],'outcome_unknown');before=len(self.server.calls)
        reconcile(self.s,self.settings,result['publication_id'],resolution='retry_authorized',reason='Operator checked absent pending insertion',base=self.server.base)
        with self.assertRaisesRegex(ValueError,'other delivery scope unresolved'):self.send(history,retry=True)
        self.assertEqual(self.writes(before),[])
        done=self.document_only(history,retry=True);self.assertCompleted(done)
        self.assertEqual(sum(c[1].endswith('/descendant') for c in self.writes(before)),1)
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))

    def test_document_only_reconcile_preserved_card_skips_get_and_rejects_message_adoption(self):
        first=self.send(self.report());self.assertCompleted(first)
        changed=self.report(blocks=[block(2,'Coverage'),callout(2,'Changed'),callout(5),block(4,'Other section')])
        result=self.document_only(changed,transport=self.ambiguous(lambda m,p:m=='PATCH'))
        self.assertEqual(result['status'],'outcome_unknown');before=len(self.server.calls)
        for remote in ({'message_id':first['message_id']},{'message_absent':True}):
            with self.assertRaisesRegex(ValueError,'does not accept message confirmations'):
                reconcile(self.s,self.settings,result['publication_id'],resolution='retry_authorized',reason='Operator checked',remote_ids=remote,base=self.server.base)
        done=reconcile(self.s,self.settings,result['publication_id'],resolution='completed',reason='Operator checked exact changed tree',base=self.server.base)
        self.assertEqual(done['publication']['message_id'],first['message_id'])
        self.assertEqual(done['publication']['status'],'completed')
        self.assertEqual(self.writes(before),[])
        self.assertFalse(any('/im/v1/messages' in c[1] for c in self.server.calls[before:]))

    def test_new_day_keeps_history_tree_ids_text_edits_and_comments(self):
        first=self.send(self.report('2026-01-14'));self.assertCompleted(first)
        old=copy.deepcopy(self.server.nodes);leaf=next(b for b in old.values() if b['block_type']==12)
        self.server.nodes[leaf['block_id']]['bullet']['elements'][0]['text_run']['content']='User historical edit'
        self.server.nodes[leaf['block_id']]['comment_ids']=['user-comment'];self.server.revision+=1
        old_callout=next(b for b in self.server.nodes.values() if b['block_type']==19)
        old_callout['callout']['background_color']=7
        preserved=copy.deepcopy(self.server.nodes);before=len(self.server.calls)
        second=self.send(self.report('2026-01-15'));self.assertCompleted(second)
        for bid,b in preserved.items():
            if bid!=self.server.root:self.assertEqual(self.server.nodes[bid],b)
        self.assertEqual(first['document_id'],second['document_id']);self.assertNotEqual(first['message_id'],second['message_id'])
        self.assertFalse(any(c[0] in {'PATCH','DELETE'} for c in self.writes(before)))
        # The old report acknowledgement must not quietly authorize overwriting
        # that edit when a later update targets the historical date.
        before=len(self.server.calls);result=self.send(self.report('2026-01-14',card='Changed old summary'))
        self.assertEqual(result['status'],'needs_user');self.assertEqual(self.writes(before),[])

    def test_same_day_structural_change_only_selected_callout_children(self):
        first=self.send(self.report());self.assertCompleted(first)
        orange=next(b for b in self.server.nodes.values() if b['block_type']==19 and b['callout']['background_color']==2)
        blue=copy.deepcopy(next(b for b in self.server.nodes.values() if b['block_type']==19 and b['callout']['background_color']==5))
        changed=callout(2);changed['child_blocks']=[block(4,'Deliverables'),block(13,'New ordered detail'),block(12,'Extra detail')]
        before=len(self.server.calls);second=self.send(self.report(blocks=[block(2,'Coverage'),changed,callout(5),block(4,'Other section')],card='Updated'))
        self.assertCompleted(second);self.assertIn(orange['block_id'],self.server.nodes);self.assertEqual(self.server.nodes[blue['block_id']],blue)
        docwrites=[c for c in self.writes(before) if c[1].startswith('/docx/')]
        self.assertTrue(docwrites)
        allowed=set(self.server.nodes[orange['block_id']]['children'])
        self.assertTrue(all(c[1].rsplit('/',1)[1] in allowed if c[0]=='PATCH' else '/blocks/'+orange['block_id']+'/' in c[1] for c in docwrites))
        self.assertEqual(second['message_id'],first['message_id'])

    def test_nested_callout_children_count_toward_limit_before_any_request(self):
        self.settings['timeline_max_blocks']=7
        result=self.send(self.report());self.assertEqual(result['status'],'needs_user')
        self.assertEqual(self.server.calls,[])

    def test_large_callout_is_seeded_then_bounded_append_without_empty_container(self):
        long=callout(2);long['child_blocks']=[block(12,'Synthetic '+str(i)) for i in range(120)]
        result=self.send(self.report(blocks=[long]));self.assertCompleted(result)
        orange=next(b for b in self.server.nodes.values() if b['block_type']==19)
        self.assertEqual(len(orange['children']),120)
        for method,path,body,_ in self.writes():
            if path.endswith('/descendant'):self.assertLessEqual(len(body['descendants']),50)
            if path.endswith('/children'):self.assertLessEqual(len(body['children']),50)

    def ambiguous(self,match,after=True):
        fired=[False]
        def transport(method,url,body,headers):
            if not fired[0] and match(method,urllib.parse.urlsplit(url).path):
                fired[0]=True
                if after:http(method,url,body,headers)
                raise AmbiguousError('synthetic request/response lost')
            return http(method,url,body,headers)
        return transport

    def recover(self,result):
        count=len(self.writes())
        with self.assertRaises(ValueError):self.send(self.s.get(result['report_id']),retry=True)
        self.assertEqual(len(self.writes()),count)
        inspected=reconcile(self.s,self.settings,result['publication_id'],base=self.server.base)
        self.assertEqual(len(self.writes()),count);self.assertIn('GET only',inspected['note'])
        reconcile(self.s,self.settings,result['publication_id'],resolution='retry_authorized',reason='Operator checked synthetic whole tree',base=self.server.base)
        self.assertEqual(len(self.writes()),count)
        completed=self.send(self.s.get(result['report_id']),retry=True);self.assertCompleted(completed)
        return completed

    def test_unknown_descendant_applied_reconcile_never_recreates_subtree(self):
        result=self.send(self.report(),transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/descendant')))
        self.assertEqual(result['status'],'outcome_unknown');ids=set(self.server.nodes)
        completed=self.recover(result);self.assertEqual(set(self.server.nodes),ids)
        self.assertEqual(sum(c[1].endswith('/descendant') for c in self.writes()),1)
        self.assertTrue(completed['message_id'])

    def test_unknown_descendant_absent_requires_separate_explicit_retry(self):
        result=self.send(self.report(),transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/descendant'),after=False))
        self.assertEqual(result['status'],'outcome_unknown');self.assertEqual(len(self.server.nodes),1)
        self.recover(result)
        self.assertEqual(sum(c[1].endswith('/descendant') for c in self.writes()),1)

    def test_unknown_created_tree_doc_uses_operator_supplied_id_without_second_create(self):
        report=self.report()
        result=self.send(report,transport=self.ambiguous(lambda m,p:m=='POST' and p=='/docx/v1/documents'))
        self.assertEqual(result['status'],'outcome_unknown')
        binding_id=result['binding_record_id']
        reconcile(self.s,self.settings,binding_id,resolution='retry_authorized',reason='Operator located newly created empty tree document',
                  remote_ids={'document_id':self.server.root},base=self.server.base)
        completed=self.send(report,retry=True);self.assertCompleted(completed)
        self.assertEqual(sum(c[0]=='POST' and c[1]=='/docx/v1/documents' for c in self.writes()),1)

    def test_partial_large_subtree_cannot_be_completed_and_resumes_only_unfinished_batch(self):
        long=callout(2);long['child_blocks']=[block(12,'Synthetic '+str(i)) for i in range(120)]
        report=self.report(blocks=[long])
        result=self.send(report,transport=self.ambiguous(lambda m,p:m=='POST' and p.endswith('/children')))
        self.assertEqual(result['status'],'outcome_unknown');known=set(self.server.nodes)
        binding_id=self.binding()['id'];before=len(self.writes())
        with self.assertRaises(ValueError):
            reconcile(self.s,self.settings,binding_id,resolution='completed',reason='Still partial',base=self.server.base)
        self.assertEqual(len(self.writes()),before)
        self.recover(result);self.assertTrue(known<=set(self.server.nodes))
        orange=next(b for b in self.server.nodes.values() if b['block_type']==19)
        self.assertEqual(len(orange['children']),120);self.assertEqual(len(set(orange['children'])),120)
        self.assertEqual(sum(c[1].endswith('/descendant') for c in self.writes()),1)

    def test_non_top_day_nested_update_keeps_newer_full_tree_and_original_day_card(self):
        first=self.send(self.report('2026-01-14'));self.assertCompleted(first)
        self.assertCompleted(self.send(self.report('2026-01-15')))
        newer=copy.deepcopy(self.binding()['data']['sections']['2026-01-15']['tree'])
        newer_ids={n['id'] for n in tree.walk(newer)};before_nodes={bid:copy.deepcopy(self.server.nodes[bid]) for bid in newer_ids}
        changed=callout(2);changed['child_blocks'].append(block(12,'Additional historical detail'))
        result=self.send(self.report('2026-01-14',blocks=[block(2,'Coverage'),changed,callout(5),block(4,'Other section')],card='Updated historical summary'))
        self.assertCompleted(result);self.assertEqual(result['message_id'],first['message_id'])
        self.assertEqual(self.binding()['data']['sections']['2026-01-15']['tree'],newer)
        for bid,b in before_nodes.items():self.assertEqual(self.server.nodes[bid],b)

    def test_unknown_leaf_patch_applied_is_not_repeated(self):
        self.assertCompleted(self.send(self.report()))
        report=self.report(blocks=[block(2,'Coverage'),callout(2,'Changed'),callout(5),block(4,'Other section')],card='Changed')
        result=self.send(report,transport=self.ambiguous(lambda m,p:m=='PATCH' and p.startswith('/docx/')))
        self.assertEqual(result['status'],'outcome_unknown');before=len(self.writes())
        self.recover(result);self.assertEqual(len(self.writes())-before,1) # card only

    def test_unknown_nested_delete_applied_is_not_repeated(self):
        self.assertCompleted(self.send(self.report()))
        orange=callout(2);orange['child_blocks']=[block(4,'Deliverables')]
        report=self.report(blocks=[block(2,'Coverage'),orange,callout(5),block(4,'Other section')],card='Changed')
        result=self.send(report,transport=self.ambiguous(lambda m,p:m=='DELETE'))
        self.assertEqual(result['status'],'outcome_unknown');before=len(self.writes());self.recover(result)
        self.assertEqual(len(self.writes())-before,1)

    def test_bad_descendant_relations_unknown_then_full_tree_resolution(self):
        def transport(method,url,body,headers):
            result=http(method,url,body,headers)
            if urllib.parse.urlsplit(url).path.endswith('/descendant'):
                result['data']['block_id_relations'].pop()
            return result
        result=self.send(self.report(),transport=transport);self.assertEqual(result['status'],'outcome_unknown')
        self.recover(result);self.assertEqual(sum(c[1].endswith('/descendant') for c in self.writes()),1)

    def test_target_edit_after_insert_stops_before_delete_and_card(self):
        self.assertCompleted(self.send(self.report()))
        orange=next(b for b in self.server.nodes.values() if b['block_type']==19 and b['callout']['background_color']==2)
        old=orange['children'][1];card=copy.deepcopy(self.server.messages)
        def edit(server,path,body):
            if path.endswith('/children'):
                server.nodes[old]['bullet']['elements'][0]['text_run']['content']='Concurrent user edit';server.revision+=1
        self.server.after_write=edit
        changed=callout(2);changed['child_blocks']=[block(4,'Deliverables'),block(13,'New type')]
        result=self.send(self.report(blocks=[block(2,'Coverage'),changed,callout(5),block(4,'Other section')]))
        self.assertEqual(result['status'],'needs_user');self.assertIn(old,self.server.nodes);self.assertEqual(self.server.messages,card)

    def surgery(self,period='2026-01-15'):
        flat=[block(2,'Coverage'),block(4,'Deliverables'),block(12,'Synthetic detail',True),block(4,'Sync'),block(12,'Synthetic detail',True),block(4,'Other section')]
        report=self.report(period,blocks=flat,tree_format=False)
        first=publish(self.s,dict(self.settings,document_layout='per_period'),report['id'],confirm=True,base=self.server.base)
        self.assertCompleted(first);ids=list(self.server.nodes[self.server.root]['children'])
        orange=self.server.add({'block_type':19,'callout':callout(2)['callout'],'children':[]},self.server.root)
        blank=self.server.add(block(2,''),orange);self.server.nodes[orange]['children']=[blank]
        self.server.insert(self.server.root,1,[orange]);baseline=self.server.snapshot()
        for i in ids[1:3]:
            self.server.nodes[i]['parent_id']=orange;self.server.nodes[orange]['children'].insert(-1,i)
        blue=self.server.add({'block_type':19,'callout':callout(5)['callout'],'children':ids[3:5]},self.server.root)
        for i in ids[3:5]:self.server.nodes[i]['parent_id']=blue
        self.server.nodes[self.server.root]['children']=[ids[0],orange,blue,ids[5]];self.server.revision+=3
        snap=self.server.snapshot();ack={'schema':'private.document-tree.v1','document_id':self.server.root,'revision':self.server.revision,
            'snapshot':snap,'hash':tree.surgery_hash(snap),'nodes':[{'id':bid,'owner':'user-adopted' if bid==orange else 'tool'} for bid in self.server.nodes]}
        journal={'status':'completed','pending':None,'document_id':self.server.root,'baseline':baseline,'document_tree':ack}
        path=self.root/'private-surgery.json';path.write_text(dumps(journal))
        data=self.s.get(first['publication_id'])['data'];data.update(layout_lock={'reason':'user callouts'},document_tree=ack)
        self.s.update_record(first['publication_id'],data)
        return first,path,orange,blank

    def test_adopt_surgery_preserves_orange_and_original_user_blank_first_publish(self):
        first,path,orange,blank=self.surgery();before=len(self.server.calls)
        adopted=adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),base=self.server.base)
        self.assertEqual(adopted['document_id'],first['document_id']);self.assertEqual(adopted['message_id'],first['message_id'])
        self.assertEqual(self.writes(before),[]);self.assertNotIn('layout_lock',self.s.get(first['publication_id'])['data'])
        nodes=list(tree.walk(self.binding()['data']['sections']['2026-01-15']['tree']))
        self.assertEqual(next(n for n in nodes if n['id']==blank)['owner'],'user-adopted')
        self.assertTrue(next(n for n in nodes if n['id']==blank)['preserved'])
        before_ids=set(self.server.nodes);before=len(self.server.calls);result=self.send(self.report());self.assertCompleted(result)
        self.assertEqual(result['message_id'],first['message_id']);self.assertTrue(before_ids<=set(self.server.nodes))
        self.assertEqual(len(self.server.nodes)-len(before_ids),1) # date h1 only
        docwrites=[c for c in self.writes(before) if c[1].startswith('/docx/')]
        self.assertEqual(len(docwrites),1);self.assertTrue(docwrites[0][1].endswith('/'+self.server.root+'/children'))
        self.assertEqual(self.server.nodes[orange]['children'][-1],blank)
        changed=self.report(blocks=[block(2,'Coverage'),callout(2,'Corrected'),callout(5),block(4,'Other section')],card='Correction')
        self.assertCompleted(self.send(changed));self.assertIn(orange,self.server.nodes);self.assertIn(blank,self.server.nodes)
        preserved=next(n for n in tree.walk(self.binding()['data']['sections']['2026-01-15']['tree']) if n['id']==blank)
        self.assertEqual(preserved['owner'],'user-adopted');self.assertTrue(preserved['preserved'])

    def test_user_owned_orange_style_or_removal_cannot_be_deleted_by_source(self):
        first,path,orange,blank=self.surgery()
        adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),base=self.server.base)
        before=len(self.server.calls)
        result=self.send(self.report(blocks=[block(2,'Coverage'),callout(5),block(4,'Other section')]))
        self.assertEqual(result['status'],'needs_user');self.assertEqual(self.writes(before),[])
        self.assertIn(orange,self.server.nodes);self.assertIn(blank,self.server.nodes)

    def test_adopt_requires_explicit_journal_and_exact_ack_tree(self):
        first,path,orange,blank=self.surgery();before=len(self.server.calls)
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,base=self.server.base)
        journal=json.loads(path.read_text());journal['status']='outcome_unknown';path.write_text(dumps(journal))
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),base=self.server.base)
        journal['status']='completed';path.write_text(dumps(journal))
        data=self.s.get(first['publication_id'])['data'];data['pending']={'stage':'unknown-effect'};self.s.update_record(first['publication_id'],data)
        with self.assertRaises(ValueError):adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),base=self.server.base)
        self.assertEqual(self.writes(before),[]);self.assertTrue(self.s.get(first['publication_id'])['data']['layout_lock'])

    def test_adopt_canonical_rename_is_explicit_and_preserves_tree(self):
        first,path,orange,blank=self.surgery();before_ids=set(self.server.nodes)
        adopted=adopt_timeline(self.s,self.settings,first['publication_id'],confirm=True,tree_journal=str(path),title='Synthetic Canonical',base=self.server.base)
        self.assertEqual(adopted['title'],'Synthetic Canonical');self.assertEqual(set(self.server.nodes),before_ids)
        self.assertCompleted(self.send(self.report()));self.assertEqual(self.server.title,'Synthetic Canonical')

    def test_per_period_tree_fails_before_any_request_without_flattening(self):
        report=self.report()
        with self.assertRaises(ValueError):publish(self.s,dict(self.settings,document_layout='per_period'),report['id'],confirm=True,base=self.server.base)
        self.assertEqual(self.server.calls,[])

    def test_flat_timeline_can_add_native_callouts_without_rebuilding_history(self):
        old=self.send(self.report('2026-01-14',blocks=[block(2,'Flat day')],tree_format=False));self.assertCompleted(old)
        ids=set(self.server.nodes);self.assertCompleted(self.send(self.report('2026-01-15')))
        self.assertTrue(ids<=set(self.server.nodes))
