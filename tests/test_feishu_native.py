"""Synthetic HTTP document service: types, ownership checks and migration recovery."""
import copy
import json
import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from work_assistant.feishu import publish, reconcile, http, AmbiguousError, TEXT_BLOCKS
from work_assistant.reports import generate_report
from work_assistant.util import dumps, digest
from test_runtime import Base


def block(kind, content, bold=False):
    return {'block_type': kind, TEXT_BLOCKS[kind]: {'elements': [
        {'text_run': {'content': content, 'text_element_style': {'bold': bold}}}]}}


class NativeServer:
    def __init__(self, multiple_messages=False):
        self.calls, self.blocks, self.counter, self.revision = [], [], 0, 1
        self.title, self.card = '', None
        self.multiple_messages, self.messages = multiple_messages, {}
        self.after_append = None
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle_request(self):
                parsed=urllib.parse.urlsplit(self.path);path=parsed.path;query=urllib.parse.parse_qs(parsed.query)
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                body=json.loads(raw) if raw else None
                outer.calls.append((self.command,path,body,query))
                data={}
                if path.endswith('/tenant_access_token/internal'):
                    response={'code':0,'tenant_access_token':'synthetic-token'}
                else:
                    if path=='/docx/v1/documents' and self.command=='POST':
                        outer.title=body['title'];data={'document':{'document_id':'doc-native'}}
                    elif path.endswith('/children') and self.command=='GET':
                        offset=int(query.get('page_token',['0'])[0]);page=copy.deepcopy(outer.blocks[offset:offset+3])
                        for item in page:
                            field=TEXT_BLOCKS[item['block_type']]
                            item[field].setdefault('style',{}).update(align=1,folded=False)
                            for element in item[field]['elements']:
                                style=element['text_run'].setdefault('text_element_style',{})
                                for key in ('bold','inline_code','italic','strikethrough','underline'):
                                    style.setdefault(key,False)
                        more=offset+3<len(outer.blocks)
                        data={'items':page,'has_more':more,'page_token':str(offset+3) if more else ''}
                    elif path.endswith('/children') and self.command=='POST':
                        created=[]
                        for value in body['children']:
                            outer.counter+=1
                            created.append(dict(copy.deepcopy(value),block_id='native-%s'%outer.counter,parent_id='doc-native'))
                        index=len(outer.blocks) if body['index']==-1 else body['index']
                        outer.blocks[index:index]=created;outer.revision+=1
                        data={'children':copy.deepcopy(created),'document_revision_id':outer.revision}
                        if body['index']==-1 and outer.after_append:
                            outer.after_append(outer);outer.revision+=1
                    elif path.endswith('/children/batch_delete'):
                        del outer.blocks[body['start_index']:body['end_index']];outer.revision+=1
                        data={'document_revision_id':outer.revision}
                    elif path.startswith('/docx/') and self.command=='PATCH':
                        ident=path.rsplit('/',1)[1]
                        if ident=='doc-native':
                            outer.title=''.join(e['text_run']['content'] for e in body['update_text_elements']['elements'])
                            item={'block_id':ident,'block_type':1,'page':{'elements':body['update_text_elements']['elements']}}
                        else:
                            item=next(b for b in outer.blocks if b['block_id']==ident)
                            item[TEXT_BLOCKS[item['block_type']]]['elements']=copy.deepcopy(body['update_text_elements']['elements'])
                        outer.revision+=1
                        data={'block':copy.deepcopy(item),'document_revision_id':outer.revision}
                    elif path.endswith('/raw_content'):
                        data={'content':outer.title+'\n'+'\n'.join(''.join(e['text_run']['content'] for e in b[TEXT_BLOCKS[b['block_type']]]['elements']) for b in outer.blocks)}
                    elif path=='/docx/v1/documents/doc-native':
                        data={'document':{'document_id':'doc-native','title':outer.title,'revision_id':outer.revision}}
                    elif path=='/docx/v1/documents/doc-native/blocks/doc-native':
                        data={'block':{'block_id':'doc-native','block_type':1,'children':[b['block_id'] for b in outer.blocks],
                                       'page':{'elements':[{'text_run':{'content':outer.title}}]}}}
                    elif path=='/im/v1/messages' and self.command=='POST':
                        ident='message-native-%s'%(len(outer.messages)+1) if outer.multiple_messages else 'message-native'
                        outer.card=json.loads(body['content']);outer.messages[ident]=outer.card;data={'message_id':ident}
                    elif path.startswith('/im/v1/messages/') and self.command=='PATCH':
                        outer.card=json.loads(body['content']);outer.messages[path.rsplit('/',1)[1]]=outer.card
                    elif path.startswith('/im/v1/messages/'):
                        ident=path.rsplit('/',1)[1]
                        data={'items':[{'message_id':ident,'body':{'content':dumps(outer.messages[ident])}}]}
                    response={'code':0,'data':data}
                encoded=dumps(response).encode();self.send_response(200);self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
            do_GET=do_POST=do_PATCH=do_DELETE=handle_request
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True);self.thread.start()
        self.base='http://127.0.0.1:%s'%self.server.server_address[1]

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()


class NativePublisherTests(Base):
    def setUp(self):
        super().setUp()
        self.server=NativeServer();self.addCleanup(self.server.close)
        env=patch.dict(os.environ,{'FEISHU_APP_ID':'synthetic-app','FEISHU_APP_SECRET':'synthetic-secret','FEISHU_RECEIVE_ID':'synthetic-self'})
        env.start();self.addCleanup(env.stop)
        self.settings=self.conf['publisher'];self.task();self.report=generate_report(self.s,self.p['id'],'2026-01-15')
        self.revise([block(2,'Source heading'),block(2,'Source paragraph'),block(2,'Source bullet')])

    def revise(self, children):
        path=self.root/('native-report-'+str(len(self.s.records('report'))));path.mkdir()
        original=self.report['data'];content={n:(Path(original['path'])/n).read_text() for n in original['files']}
        doc=json.loads(content['document.json']);doc['children']=children;content['document.json']=dumps(doc)
        for name,text in content.items():(path/name).write_text(text)
        self.report=self.s.record('report',dict(original,path=str(path),hash=digest(content),files={n:digest(t.encode()) for n,t in content.items()}),project_id=self.p['id'])

    def send(self, **kwargs):
        return publish(self.s,self.settings,self.report['id'],confirm=True,base=self.server.base,**kwargs)

    def native(self):
        return [block(4,'Native heading'),block(12,'Native bullet',True),block(5,'Native detail'),block(13,'Native ordered')]

    def test_typed_create_and_same_type_elements_update_keep_bold(self):
        self.revise(self.native());first=self.send();self.assertEqual(first['status'],'completed')
        self.assertEqual([b['block_type'] for b in self.server.blocks],[4,12,5,13])
        self.assertTrue(self.server.blocks[1]['bullet']['elements'][0]['text_run']['text_element_style']['bold'])
        updated=self.native();updated[0]['heading2']['elements'][0]['text_run']['content']='Updated heading'
        self.revise(updated);before=len(self.server.calls);second=self.send()
        self.assertEqual(second['block_ids'],first['block_ids'])
        edits=[c for c in self.server.calls[before:] if c[0]=='PATCH' and c[1].startswith('/docx/')]
        self.assertEqual(len(edits),4)
        self.assertTrue(all('update_text_elements' in c[2] for c in edits))
        self.assertNotIn('document_migration',second)

    def test_all_supported_heading_and_list_types_accept_native_runs(self):
        kinds=sorted(TEXT_BLOCKS)
        self.revise([block(kind,'Synthetic native type %s'%kind,True) for kind in kinds])
        self.assertEqual(self.send()['status'],'completed')
        self.assertEqual([b['block_type'] for b in self.server.blocks],kinds)
        for value in self.server.blocks:
            self.assertTrue(value[TEXT_BLOCKS[value['block_type']]]['elements'][0]['text_run']['text_element_style']['bold'])

    def test_legacy_ack_snapshot_migrates_and_malformed_pagination_refuses(self):
        first=self.send()
        data=self.s.get(first['publication_id'])['data']
        data.pop('document_report_id');data.pop('document_payload_hash')
        self.s.update_record(first['publication_id'],data)
        self.revise(self.native())
        def bad_pages(method,url,body,headers):
            value=http(method,url,body,headers)
            if method=='GET' and '/children?' in url:value['data']['has_more']='false'
            return value
        before=len(self.server.calls)
        failed=self.send(transport=bad_pages)
        self.assertEqual(failed['status'],'failed')
        self.assertFalse(any(c[0] in {'DELETE','PATCH'} or (c[0]=='POST' and c[1].endswith('/children')) for c in self.server.calls[before:]))
        self.assertEqual(failed['document_report_id'],first['report_id'])
        self.assertEqual(self.send(retry=True)['status'],'completed')

    def test_plain_to_native_batches_then_deletes_old_same_doc_and_card(self):
        first=self.send();self.revise(self.native());before=len(self.server.calls);second=self.send()
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['document_id'],first['document_id']);self.assertEqual(second['message_id'],first['message_id'])
        self.assertEqual(second['document_migration']['old_block_ids'],first['block_ids'])
        self.assertEqual(second['document_migration']['new_block_ids'],second['block_ids'])
        self.assertEqual(second['document_migration']['state'],'complete')
        self.assertEqual([b['block_id'] for b in self.server.blocks],second['block_ids'])
        calls=self.server.calls[before:]
        append=[c for c in calls if c[0]=='POST' and c[1].endswith('/children')]
        self.assertEqual(len(append),1);self.assertEqual(len(append[0][2]['children']),4);self.assertEqual(append[0][2]['index'],-1)
        self.assertIn('client_token',append[0][3]);self.assertIn('document_revision_id',append[0][3])
        delete=next(c for c in calls if c[0]=='DELETE');self.assertEqual(delete[2],{'start_index':0,'end_index':3})
        self.assertFalse(any(c[0]=='PATCH' and c[1].startswith('/docx/') for c in calls))
        count=len(self.server.calls);self.assertTrue(self.send()['no_op']);self.assertEqual(len(self.server.calls),count)

    def test_large_native_migration_uses_at_most_fifty_per_append(self):
        self.send();self.revise([block(4 if i==0 else 12,'Synthetic %s'%i) for i in range(55)])
        before=len(self.server.calls);result=self.send();self.assertEqual(result['status'],'completed')
        batches=[len(c[2]['children']) for c in self.server.calls[before:] if c[0]=='POST' and c[1].endswith('/children')]
        self.assertEqual(batches,[50,5])

    def test_remote_edits_unexpected_children_comments_or_styles_refuse_before_append(self):
        self.send();self.revise(self.native())
        original=copy.deepcopy(self.server.blocks)
        for edit in ('content','style','extra','nested','comment'):
            with self.subTest(edit=edit):
                self.server.blocks=copy.deepcopy(original)
                if edit=='content':self.server.blocks[0]['text']['elements'][0]['text_run']['content']='User edit'
                elif edit=='style':self.server.blocks[0]['text']['elements'][0]['text_run']['text_element_style']['italic']=True
                elif edit=='extra':self.server.blocks.append(dict(block(2,'User block'),block_id='user-added',parent_id='doc-native'))
                elif edit=='nested':self.server.blocks[0]['children']=['user-nested']
                else:self.server.blocks[0]['comment_ids']=['user-comment']
                before=len(self.server.calls);result=self.send(retry=True)
                self.assertEqual(result['status'],'failed')
                self.assertFalse(any(c[0] in {'POST','DELETE','PATCH'} and '/tenant_access_token/' not in c[1] for c in self.server.calls[before:]))

    def test_edit_after_append_is_detected_before_delete_or_card_patch(self):
        first=self.send();self.revise(self.native())
        self.server.after_append=lambda server:server.blocks[0]['text']['elements'][0]['text_run'].update(content='Concurrent user edit')
        before=len(self.server.calls);result=self.send()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['document_migration']['old_block_ids'],first['block_ids'])
        self.assertFalse(any(c[0] in {'DELETE','PATCH'} for c in self.server.calls[before:]))
        self.assertEqual(len(self.server.blocks),7)

    def test_unknown_append_requires_read_only_inspection_and_explicit_adoption(self):
        self.send();self.revise(self.native())
        def lost_response(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='POST' and '/children?' in url:raise AmbiguousError('synthetic response lost after append')
            return result
        unknown=self.send(transport=lost_response);self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(unknown['document_migration']['new_block_ids'],[])
        self.assertEqual(len(self.server.blocks),7)
        count=len(self.server.calls)
        with self.assertRaises(ValueError):self.send(retry=True)
        self.assertEqual(len(self.server.calls),count)
        inspected=reconcile(self.s,self.settings,unknown['publication_id'],base=self.server.base)
        self.assertEqual(inspected['inspection']['migration']['state'],'ready_delete')
        self.assertEqual(self.s.get(unknown['publication_id'])['data']['status'],'outcome_unknown')
        with self.assertRaises(ValueError):reconcile(self.s,self.settings,unknown['publication_id'],'completed','partial migration is not complete',base=self.server.base)
        reconcile(self.s,self.settings,unknown['publication_id'],'retry_authorized','operator verified appended native prefix and all old IDs',base=self.server.base)
        before=len(self.server.calls);done=self.send(retry=True);self.assertEqual(done['status'],'completed')
        self.assertFalse(any(c[0]=='POST' and c[1].endswith('/children') for c in self.server.calls[before:]))
        self.assertEqual(len(self.server.blocks),4)

    def test_unknown_delete_applied_reconciles_to_new_ids_without_second_delete(self):
        self.send();self.revise(self.native())
        def lost_delete(method,url,body,headers):
            result=http(method,url,body,headers)
            if method=='DELETE':raise AmbiguousError('synthetic response lost after deletion')
            return result
        unknown=self.send(transport=lost_delete);self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(len(self.server.blocks),4)
        inspected=reconcile(self.s,self.settings,unknown['publication_id'],base=self.server.base)
        self.assertEqual(inspected['inspection']['migration']['state'],'complete')
        reconcile(self.s,self.settings,unknown['publication_id'],'retry_authorized','operator verified only new native IDs remain',base=self.server.base)
        before=len(self.server.calls);done=self.send(retry=True);self.assertEqual(done['status'],'completed')
        self.assertFalse(any(c[0]=='DELETE' or (c[0]=='POST' and c[1].endswith('/children')) for c in self.server.calls[before:]))
        self.assertEqual([b['block_id'] for b in self.server.blocks],done['block_ids'])

    def test_unknown_append_with_no_effect_can_only_replay_after_explicit_resolution(self):
        self.send();self.revise(self.native())
        def no_effect(method,url,body,headers):
            if method=='POST' and '/children?' in url:raise AmbiguousError('synthetic lost connection before receipt')
            return http(method,url,body,headers)
        unknown=self.send(transport=no_effect);self.assertEqual(unknown['status'],'outcome_unknown')
        self.assertEqual(len(self.server.blocks),3)
        reconcile(self.s,self.settings,unknown['publication_id'],'cancelled','operator stops',base=self.server.base)
        with self.assertRaises(ValueError):self.send(retry=True)
        reconcile(self.s,self.settings,unknown['publication_id'],'retry_authorized','operator verified unchanged old blocks and no appended blocks',base=self.server.base)
        self.assertEqual(self.send(retry=True)['status'],'completed')
        self.assertEqual(len(self.server.blocks),4)
