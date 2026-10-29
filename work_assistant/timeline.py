"""Optional fixed-document daily sections. No scheduling, implicit adoption or retry."""
import copy
import urllib.parse
import uuid
from datetime import date
from .feishu import APIError, AmbiguousError, make_client, preview, block_signature, document_card, TEXT_BLOCKS
from .util import digest, dumps, redact


class NeedsUser(APIError):
    pass


def binding_key(destination, project):
    return digest([destination, project, 'timeline-v1'])


def find_binding(store, key):
    return next((r for r in store.records('doc_binding') if r['data']['binding_id'] == key), None)


def root_ids(binding):
    return [i for day in binding['order'] for i in binding['sections'][day]['block_ids']]


def guard_layout(store, binding):
    if binding.get('layout_lock'):
        raise NeedsUser('document layout is locked; flat timeline cannot modify or adopt its tree')
    document_id = binding.get('document_id')
    if document_id and any(r['data'].get('document_id') == document_id and r['data'].get('layout_lock')
                           for r in store.records('publication', project_id=binding['project_id'])):
        raise NeedsUser('document has an explicit publication layout_lock; tree support and explicit adoption required')


def section_blocks(payload, period):
    if date.fromisoformat(period).isoformat() != period:
        raise ValueError('timeline period must be YYYY-MM-DD')
    from .document_tree import local_blocks
    return [{'block_type': 3, 'heading1': {'elements': [{'text_run': {'content': period}}]}}] + local_blocks(payload['document'])


def binding_observe(client, binding, title=None):
    if binding.get('tree_mode'):
        from .document_tree import observe as tree_observe, roots, same_nodes
        actual=tree_observe(client,binding['document_id'])
        if title is not None and actual['title']!=title:raise NeedsUser('timeline title differs from acknowledgement')
        try:same_nodes(actual['nodes'],roots(binding),())
        except APIError as exc:raise NeedsUser(str(exc)) from None
        return actual
    return observe(client,binding['document_id'],expected=root_ids(binding),title=title)


def document_engine(store, client, record):
    if record['data'].get('tree_mode'):
        from .timeline_tree import TreeDocument
        return TreeDocument(store,client,record)
    return Document(store,client,record)


def observe(client, document_id, ranges=(), expected=None, title=None):
    """Full root ID list, paginated target prefix only, stable metadata revision."""
    doc = urllib.parse.quote(document_id, safe='')
    before = client.request('GET', '/docx/v1/documents/' + doc).get('document', {})
    revision = before.get('revision_id')
    if type(revision) is not int or revision < 1:
        raise NeedsUser('timeline document revision unavailable')
    page = client.request('GET', '/docx/v1/documents/%s/blocks/%s' % (doc, doc)).get('block', {})
    ids = page.get('children', [])
    if (type(page.get('block_type')) is not int or page.get('block_type') != 1 or page.get('block_id') != document_id or type(ids) is not list or
            any(type(i) is not str or not i for i in ids) or len(ids) != len(set(ids)) or len(ids) > 100000):
        raise NeedsUser('invalid or duplicate full-root timeline IDs')
    if expected is not None and ids != expected:
        raise NeedsUser('timeline root structure differs from acknowledged sections; no writes allowed')
    if title is not None and before.get('title') != title:
        raise NeedsUser('timeline title differs from acknowledged title; use explicit inspection/rename')
    end = max((b for a, b in ranges), default=0)
    if end > len(ids):
        raise NeedsUser('timeline target range exceeds root children')
    blocks, token, seen = [], None, set()
    while len(blocks) < end:
        params = {'document_revision_id': '-1', 'page_size': '500'}
        if token:
            params['page_token'] = token
        result = client.request('GET', '/docx/v1/documents/%s/blocks/%s/children?' % (doc, doc) + urllib.parse.urlencode(params))
        items = result.get('items')
        if type(items) is not list or type(result.get('has_more')) is not bool or not items:
            raise NeedsUser('incomplete timeline child pagination')
        blocks.extend(items)
        if len(blocks) > len(ids) or [b.get('block_id') for b in blocks] != ids[:len(blocks)]:
            raise NeedsUser('paginated timeline IDs differ from full-root IDs')
        if not result['has_more']:
            if len(blocks) != len(ids):
                raise NeedsUser('timeline full-root/pagination count mismatch')
            break
        token = result.get('page_token')
        if type(token) is not str or not token or token in seen:
            raise NeedsUser('invalid timeline pagination token')
        seen.add(token)
    signatures = {}
    for start, stop in ranges:
        for i in range(start, stop):
            if blocks[i].get('parent_id') != document_id:
                raise NeedsUser('unexpected nested timeline target block')
            try:
                signatures[i] = block_signature(blocks[i])
            except (APIError, ValueError, TypeError):
                raise NeedsUser('timeline target has comments, nested content or unsupported edits; no writes allowed') from None
    after = client.request('GET', '/docx/v1/documents/' + doc).get('document', {})
    if after.get('revision_id') != revision or after.get('title') != before.get('title'):
        raise NeedsUser('timeline changed during read; no mutation authorized')
    return {'ids': ids, 'signatures': signatures, 'revision': revision, 'title': before.get('title')}


def operation_view(client, binding):
    """Classify the exact effects of a pending operation without changing records."""
    op = binding['operation']
    if op.get('kind')=='tree':
        from .timeline_tree import operation_view as tree_operation_view
        return tree_operation_view(client,binding)
    before = op['prefix'] + op['old_ids'] + op['suffix']
    observed = observe(client, binding['document_id'])
    ids, pos = observed['ids'], len(op['prefix'])
    if observed['title'] != binding['title']:
        raise NeedsUser('timeline title edited during section operation')
    if op['kind'] == 'patch':
        if ids != before:
            raise NeedsUser('timeline structure changed during patch')
        actual = observe(client, binding['document_id'], [(pos, pos + len(op['old_ids']))], ids, binding['title'])
        acknowledged = set(op.get('patched', []))
        pending_index = (binding.get('pending') or {}).get('block_index')
        patched = []
        for i, (old, new) in enumerate(zip(op['old_signatures'], op['target_signatures'])):
            value = actual['signatures'][pos + i]
            allowed = [new] if i in acknowledged else ([old, new] if i == pending_index else [old])
            if value not in allowed:
                raise NeedsUser('timeline target text/styles changed outside acknowledged patch')
            if value == new:
                patched.append(i)
        return dict(actual, phase='complete' if len(patched) == len(op['old_ids']) else 'patching',
                    new_ids=list(op['old_ids']), patched=patched)
    new_ids = op['new_ids']
    complete_ids = op['prefix'] + new_ids + op['suffix']
    if len(new_ids) == len(op['target_signatures']) and ids == complete_ids:
        actual = observe(client, binding['document_id'], [(pos, pos + len(new_ids))], ids, binding['title'])
        if [actual['signatures'][pos+i] for i in range(len(new_ids))] != op['target_signatures']:
            raise NeedsUser('timeline replacement content changed')
        return dict(actual, phase='complete', new_ids=list(new_ids))
    count = len(ids) - len(before)
    limit = len(new_ids) + op.get('pending_batch_count', 0)
    if not len(new_ids) <= count <= min(len(op['target_signatures']), limit):
        raise NeedsUser('timeline partial insertion size is not explained by pending request')
    inserted = ids[pos:pos+count]
    expected = op['prefix'] + inserted + op['old_ids'] + op['suffix']
    if ids != expected or inserted[:len(new_ids)] != new_ids:
        raise NeedsUser('timeline insertion has unexpected old/new IDs')
    actual = observe(client, binding['document_id'], [(pos, pos+count+len(op['old_ids']))], ids, binding['title'])
    wanted = op['target_signatures'][:count] + op['old_signatures']
    if [actual['signatures'][pos+i] for i in range(len(wanted))] != wanted:
        raise NeedsUser('timeline inserted/old section content or styles were edited')
    phase = ('ready_delete' if op['old_ids'] else 'complete') if count == len(op['target_signatures']) else 'inserting'
    return dict(actual, phase=phase, new_ids=inserted)


class Document:
    def __init__(self, store, client, record):
        self.store, self.client, self.record = store, client, record
        self.data = record['data']

    def save(self):
        self.store.update_record(self.record['id'], self.data)

    def effect(self, op, method, path, body, **detail):
        self.client.authenticate()  # No mutation intent until authentication succeeded.
        self.data.update(status='publishing', pending=dict(detail, op=op, method=method, path=path, body_hash=digest(body)))
        self.save()
        return self.client.request(method, path, body)

    def ack(self):
        pending = self.data.get('pending')
        if pending:
            self.data['steps'].append({k: pending[k] for k in ('op', 'body_hash', 'path')})
        self.data['pending'] = None
        self.save()

    def fail(self, exc):
        self.data['status'] = 'needs_user' if isinstance(exc, NeedsUser) else ('failed' if isinstance(exc, APIError) else 'outcome_unknown')
        self.data['error'] = redact(str(exc)) if isinstance(exc, APIError) else 'timeline operation interrupted/ambiguous; inspect before explicit recovery'
        self.save()

    def create(self):
        data = self.data
        if not data['document_id']:
            body = {'title': data['title']}
            folder = self.client.env('folder_token_env', False)
            if folder: body['folder_token'] = folder
            value = self.effect('create_doc', 'POST', '/docx/v1/documents', body)
            ident = value.get('document', {}).get('document_id')
            if type(ident) is not str or not ident:
                raise AmbiguousError('timeline document creation missing ID')
            data['document_id'] = ident
            self.ack()
        member = self.client.document_member()
        if member:
            perm = self.client.settings.get('document_member_perm', 'edit')
            fingerprint = digest([member, perm])
            if data.get('member_fingerprint') not in (None, fingerprint):
                raise NeedsUser('timeline member identity changed')
            if data.get('member_fingerprint') != fingerprint:
                if data.get('operation'):
                    operation_view(self.client, data)
                else:
                    binding_observe(self.client, data, title=data['title'])
                doc = urllib.parse.quote(data['document_id'], safe='')
                self.effect('share', 'POST', '/drive/v1/permissions/%s/members?type=docx&need_notification=false' % doc,
                    {'member_type':'openid', 'member_id':member, 'perm':perm, 'type':'user'})
                data['member_fingerprint'] = fingerprint
                self.ack()

    def start_section(self, period, report, payload, max_blocks):
        data = self.data
        target = section_blocks(payload, period)
        signatures = [block_signature(b) for b in target]
        old = data['sections'].get(period)
        pos = sum(len(data['sections'][day]['block_ids']) for day in data['order'] if day > period)
        baseline = root_ids(data)
        old_ids = old['block_ids'] if old else []
        old_signatures = old['signatures'] if old else []
        final_count = len(baseline) - len(old_ids) + len(target)
        same_type = len(old_ids) == len(target) and all(a['block_type'] == b['block_type'] and a['style'] == b['style'] for a,b in zip(old_signatures, signatures))
        peak = final_count if same_type else len(baseline) + len(target)
        if peak > max_blocks:
            raise NeedsUser('local timeline block limit exceeded (including temporary replacement); archive explicitly or raise local limit')
        actual = observe(self.client, data['document_id'], [(pos,pos+len(old_ids))] if old_ids else [], baseline, data['title'])
        if [actual['signatures'][pos+i] for i in range(len(old_ids))] != old_signatures:
            raise NeedsUser('target day contains user edits/comments; no writes allowed')
        op = {'id':str(uuid.uuid4()), 'kind':'patch' if same_type else ('replace' if old else 'insert'),
              'phase':'patching' if same_type else 'inserting', 'date':period, 'report_id':report['id'],
              'report_hash':report['data']['hash'], 'section_hash':digest(target), 'target_signatures':signatures,
              'prefix':baseline[:pos], 'suffix':baseline[pos+len(old_ids):], 'old_ids':list(old_ids),
              'old_signatures':copy.deepcopy(old_signatures), 'new_ids':list(old_ids) if same_type else [],
              'patched':[], 'pending_batch_count':0, 'initial_revision':actual['revision']}
        data.update(operation=op, status='publishing')
        self.save()

    def finish_section(self):
        data, op = self.data, self.data['operation']
        target = section_blocks(preview(self.store, op['report_id']), op['date'])
        if digest(target) != op['section_hash']:
            raise NeedsUser('timeline target immutable payload mismatch')
        doc = urllib.parse.quote(data['document_id'], safe='')
        view = operation_view(self.client, data)
        if view['new_ids'] != op['new_ids']:
            raise NeedsUser('unacknowledged insertion IDs need explicit reconciliation')
        pos = len(op['prefix'])
        if op['kind'] == 'patch':
            for i, block in enumerate(target):
                if op['old_signatures'][i] == op['target_signatures'][i] or i in op['patched']:
                    continue
                token = str(uuid.uuid5(uuid.UUID(op['id']), 'patch:%s' % i))
                path = '/docx/v1/documents/%s/blocks/%s?client_token=%s' % (doc, urllib.parse.quote(op['old_ids'][i],safe=''),token)
                field = TEXT_BLOCKS[block['block_type']]
                self.effect('patch', 'PATCH', path, {'update_text_elements':{'elements':block[field]['elements']}},
                            date=op['date'], block_index=i, expected_ids_digest=digest(root_ids(data)), client_token=token)
                op['patched'].append(i)
                self.ack()
        else:
            while len(op['new_ids']) < len(target):
                offset = len(op['new_ids']);batch = target[offset:offset+50]
                token = str(uuid.uuid5(uuid.UUID(op['id']), 'insert:%s' % offset))
                op['pending_batch_count'] = len(batch)
                self.save()
                query = urllib.parse.urlencode({'document_revision_id':view['revision'],'client_token':token})
                value = self.effect('insert' if op['kind']=='insert' else 'replace_insert','POST',
                    '/docx/v1/documents/%s/blocks/%s/children?%s' % (doc,doc,query), {'children':batch,'index':pos+offset},
                    date=op['date'],index=pos+offset,batch_offset=offset,batch_count=len(batch),client_token=token,
                    expected_ids_digest=digest(op['prefix']+op['new_ids']+op['old_ids']+op['suffix']))
                ids = [b.get('block_id') for b in value.get('children',[]) if type(b) is dict]
                if len(ids)!=len(batch) or any(type(i) is not str or not i for i in ids) or len(set(ids))!=len(ids) or set(ids)&set(op['prefix']+op['new_ids']+op['old_ids']+op['suffix']):
                    raise AmbiguousError('timeline insertion response missing unique IDs')
                op['new_ids'] += ids;op['pending_batch_count'] = 0
                acknowledged_revision = value.get('document_revision_id')
                op['last_ack_revision'] = acknowledged_revision if type(acknowledged_revision) is int else None
                self.ack()
                view = operation_view(self.client, data)
                if op['last_ack_revision'] is not None and view['revision'] != op['last_ack_revision']:
                    raise NeedsUser('timeline changed after insertion acknowledgement; inspect before deletion')
            if op['old_ids']:
                view = operation_view(self.client, data)
                if view['phase'] != 'complete':
                    if view['phase']!='ready_delete':raise NeedsUser('timeline replacement not ready for deletion')
                    token=str(uuid.uuid5(uuid.UUID(op['id']),'delete-old'))
                    query=urllib.parse.urlencode({'document_revision_id':view['revision'],'client_token':token})
                    op['phase']='deleting';self.save()
                    start=pos+len(op['new_ids'])
                    deleted = self.effect('replace_delete','DELETE','/docx/v1/documents/%s/blocks/%s/children/batch_delete?%s'%(doc,doc,query),
                                {'start_index':start,'end_index':start+len(op['old_ids'])},date=op['date'],index=start,
                                expected_ids_digest=digest(view['ids']),client_token=token)
                    op['phase']='verifying';op['last_ack_revision']=deleted.get('document_revision_id');self.ack()
        view=operation_view(self.client,data)
        if view['phase']!='complete':raise NeedsUser('timeline section not completely verified; no card written')
        if op.get('last_ack_revision') is not None and view['revision'] != op['last_ack_revision']:
            raise NeedsUser('timeline changed before final verification')
        self.commit_section(view)

    def commit_section(self, view, persist=True):
        data,op=self.data,self.data['operation']
        data['sections'][op['date']]={'block_ids':list(view['new_ids']), 'signatures':op['target_signatures'],
            'report_id':op['report_id'],'report_hash':op['report_hash'],'section_hash':op['section_hash']}
        data['order']=sorted(data['sections'],reverse=True)
        data.update(status='completed',pending=None,operation=None,ack_revision=view['revision'])
        data.pop('error',None)
        event={'action':'timeline_section_committed','binding_id':data['binding_id'],
            'date':op['date'],'old_ids':op['old_ids'],'new_ids':list(view['new_ids']),'report_id':op['report_id']}
        if persist:
            with self.store.transaction():
                self.save()
                self.store.record('event',event,project_id=data['project_id'])
        return event

    def rename(self, title):
        data=self.data
        if data.get('rename_request') and data['rename_request']['new_title']!=title:
            raise NeedsUser('finish or cancel existing title operation before changing its target')
        if title==data['title']:
            binding_observe(self.client,data,title=data['title'])
            return False
        request=data.get('rename_request') or {'old_title':data['title'],'new_title':title,'client_token':str(uuid.uuid4())}
        data['rename_request']=request;self.save()
        binding_observe(self.client,data,title=data['title'])
        doc=urllib.parse.quote(data['document_id'],safe='')
        self.effect('rename','PATCH','/docx/v1/documents/%s/blocks/%s?client_token=%s'%(doc,doc,request['client_token']),
                    {'update_text_elements':{'elements':[{'text_run':{'content':title}}]}},old_title=data['title'],new_title=title)
        actual=binding_observe(self.client,data)
        if actual['title']!=title:raise AmbiguousError('timeline title update not verified')
        data.update(title=title,rename_request=None,status='completed',ack_revision=actual['revision'])
        self.ack();return True


def _new_binding(store, key, destination, project, title):
    return store.record('doc_binding', {'schema':'wa.timeline.v1','binding_id':key,'destination':destination,
        'project_id':project,'document_id':None,'title':title,'order':[],'sections':{},'status':'completed',
        'pending':None,'operation':None,'steps':[],'ack_revision':None,'adopted_from':None},
        project_id=project, ident='doc_binding_'+key[:32])


def _card_identity(destination, report):
    return digest([destination, report['project_id'], report['data']['period']])


def _authority(settings, destination, confirm):
    grants=settings.get('grants',[])
    if not confirm and (type(grants) is not list or destination not in grants or any(type(g) is not str for g in grants)):
        raise ValueError('live publish requires --confirm or an exact destination grant')


def publish_timeline(store, settings, report_id, confirm=False, transport=None, base=None, retry=False, process=None):
    from .feishu import http, BASE, send_card
    payload=preview(store,report_id);report=store.get(report_id)
    client=make_client(settings, transport or http, base or BASE, process)
    destination=client.destination();_authority(settings,destination,confirm)
    identity=_card_identity(destination,report);key=binding_key(destination,report['project_id'])
    with store.claim(['doc:'+key,'publish:'+identity]):
        # Reload both records only after acquiring the shared document lock.
        record=find_binding(store,key)
        prior=next((r for r in store.records('publication') if r['data']['identity']==identity),None)
        if prior and prior['data'].get('layout_lock'):
            raise NeedsUser('publication layout_lock prevents flat timeline publication')
        if record:guard_layout(store, record['data'])
        if prior and prior['data'].get('layout')!='timeline':
            raise ValueError('existing per-period publication requires explicit publish adopt-timeline first')
        if record and record['data']['status'] in {'publishing','outcome_unknown','needs_user','cancelled'}:
            raise ValueError('timeline binding unresolved; reconcile '+record['id']+' before publishing')
        if record and record['data']['status'] in {'failed','retry_authorized'} and not retry:
            raise ValueError('timeline continuation requires explicit --retry')
        if prior and prior['data']['status'] in {'publishing','outcome_unknown','needs_user'}:
            raise ValueError('daily card outcome unresolved; reconcile publication before retry')
        if prior and prior['data'].get('outcome_unresolved'):
            raise ValueError('daily card ambiguity not resolved; cancellation is not replay permission')
        if prior and prior['data']['status'] in {'failed','retry_authorized','cancelled'} and not retry:
            raise ValueError('daily card continuation requires explicit --retry')
        if not record:
            record=_new_binding(store,key,destination,report['project_id'],settings.get('timeline_title','Daily Sync'))
        binding=record['data'];period=report['data']['period']
        if binding.get('rename_request'):
            raise ValueError('pending explicit timeline rename must be reconciled/finished first')
        wanted=section_blocks(payload,period);wanted_hash=digest(wanted)
        section=binding['sections'].get(period)
        pubhash=digest([payload['hash'],'feishu-docx-button-v1'])
        if (binding['status']=='completed' and not binding.get('operation') and section and section['section_hash']==wanted_hash and
                prior and prior['data']['status']=='completed' and prior['data'].get('hash')==pubhash):
            return dict(prior['data'],publication_id=prior['id'],no_op=True)
        data=copy.deepcopy(prior['data']) if prior else {'identity':identity,'destination':destination,'document_id':None,
            'message_id':None,'block_ids':[],'mode':'app','steps':[]}
        data.update(status='publishing',layout='timeline',binding_id=key,binding_record_id=record['id'],
                    hash=pubhash,report_hash=payload['hash'],report_id=report_id,pending=None,block_ids=[])
        data.pop('error',None)
        publication=store.record('publication',data,project_id=report['project_id'])
        binding['active_publication']=publication['id']
        if payload['document'].get('format')=='wa.document.tree.v1':
            from .timeline_tree import ensure_tree
            ensure_tree(binding)
        engine=document_engine(store,client,record);engine.save()
        document_committed=False
        try:
            if binding.get('tree_mode'):
                if not binding.get('operation'):
                    engine.preflight(period,report,payload,settings.get('timeline_max_blocks',3000))
            else:
                current_total=len(root_ids(binding));old_size=len(section['block_ids']) if section else 0
                same=section and len(section['signatures'])==len(wanted) and all(a['block_type']==b['block_type'] and a['style']==b['style'] for a,b in zip(section['signatures'],map(block_signature,wanted)))
                peak=current_total-old_size+len(wanted) if same else current_total+len(wanted)
                if (not section or section['section_hash']!=wanted_hash) and peak>settings.get('timeline_max_blocks',3000):
                    raise NeedsUser('local timeline block limit exceeded; no automatic history deletion')
            engine.create()
            data['document_id']=binding['document_id'];store.update_record(publication['id'],data)
            if binding.get('operation'):
                if binding['operation']['section_hash']!=wanted_hash or binding['operation']['date']!=period:
                    raise NeedsUser('finish pending timeline section before another day/revision')
                engine.finish_section()
            elif not section or section['section_hash']!=wanted_hash:
                engine.start_section(period,report,payload,settings.get('timeline_max_blocks',3000))
                engine.finish_section()
            else:
                if binding.get('tree_mode'):
                    from .document_tree import observe as tree_observe, same_nodes, roots
                    actual=tree_observe(client,binding['document_id'])
                    if actual['title']!=binding['title']:raise NeedsUser('tree timeline title edited')
                    try:same_nodes(actual['nodes'],roots(binding),set(section['block_ids']))
                    except APIError as exc:raise NeedsUser(str(exc)) from None
                else:
                    pos=sum(len(binding['sections'][day]['block_ids']) for day in binding['order'] if day>period)
                    actual=observe(client,binding['document_id'],[(pos,pos+len(section['block_ids']))],root_ids(binding),binding['title'])
                    if [actual['signatures'][pos+i] for i in range(len(section['block_ids']))]!=section['signatures']:
                        raise NeedsUser('target day edited before card-only update; no writes allowed')
                section.update(report_id=report_id, report_hash=report['data']['hash'])
                binding.update(status='completed',pending=None,ack_revision=actual['revision']);engine.save()
            document_committed=True
            if binding.get('document_tree'):data['document_tree']=copy.deepcopy(binding['document_tree'])
            card=document_card(payload['card'],binding['document_id'])
            card_hash=digest(card)
            prior_card_hash=data.get('card_payload_hash')
            if not prior_card_hash and prior and prior['data'].get('report_id'):
                prior_card_hash=digest(document_card(preview(store,prior['data']['report_id'])['card'],binding['document_id']))
            if not data['message_id'] or card_hash!=prior_card_hash:
                def effect(stage,method,path,body):
                    client.authenticate()
                    data['pending']={'stage':stage,'method':method,'path':path,'body_hash':digest(body)}
                    store.update_record(publication['id'],data)
                    value=client.request(method,path,body)
                    data['steps'].append({'stage':stage,'body_hash':digest(body)})
                    data['pending']=None
                    return value
                send_card(client,effect,data,card,settings)
            data.update(status='completed',pending=None,document_id=binding['document_id'],card_payload_hash=card_hash,
                        document_payload_hash=digest(payload['document']),document_report_id=report_id)
            store.update_record(publication['id'],data)
        except BaseException as exc:
            if not document_committed:
                engine.fail(exc)
                data['status']=binding['status']
            else:
                data['status']='failed' if isinstance(exc,APIError) else 'outcome_unknown'
                if not isinstance(exc,APIError):data['outcome_unresolved']=True
            data.update(document_id=binding['document_id'],error=redact(str(exc)) if isinstance(exc,APIError) else 'timeline/card outcome ambiguous; inspect before retry')
            store.update_record(publication['id'],data)
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        return dict(data,publication_id=publication['id'])


def adopt_timeline(store,settings,publication_id,confirm=False,title=None,transport=None,base=None,process=None,tree_journal=None):
    from .feishu import http,BASE,document_blocks_snapshot
    if not confirm:raise ValueError('adoption requires explicit --confirm')
    if title is not None and (type(title) is not str or not title.strip() or len(title)>200):
        raise ValueError('adoption title must be nonempty and at most 200 characters')
    if settings.get('mode')!='app' or settings.get('document_layout','per_period')!='timeline':
        raise ValueError('adoption requires configured app timeline layout')
    client=make_client(settings,transport or http,base or BASE,process)
    original=store.get(publication_id)
    if original['kind']!='publication':raise ValueError('adoption requires a completed publication')
    destination=client.destination();key=binding_key(destination,original['project_id'])
    with store.claim(['doc:'+key,'publish:'+original['data']['identity']]):
        original=store.get(publication_id);data=original['data']
        if data.get('layout_lock') and not tree_journal:
            raise NeedsUser('locked document cannot be adopted by the flat timeline implementation')
        if (data['destination']!=destination or data['status']!='completed' or data.get('pending') or data.get('outcome_unresolved') or
                not data.get('document_id') or not data.get('message_id')):
            raise ValueError('adoption requires matching completed app document and card')
        migration=data.get('document_migration')
        if migration and migration['state']!='complete':raise ValueError('unfinished native migration cannot be adopted')
        member=client.document_member()
        if member and data.get('document_member_fingerprint')!=digest([member,settings.get('document_member_perm','edit')]):
            raise ValueError('adoption requires the configured member grant to have been acknowledged already')
        if find_binding(store,key):raise ValueError('timeline already bound; use explicit rename-timeline for title changes')
        source_id=data.get('document_report_id') or data['report_id'];source=store.get(source_id)
        if source['project_id']!=original['project_id']:raise ValueError('adoption source project mismatch')
        payload=preview(store,source_id)
        if tree_journal:
            from .timeline_tree import adopt_surgery
            actual=adopt_surgery(store,client,original,payload,tree_journal)
        else:
            if payload['document'].get('format'):
                raise NeedsUser('native tree adoption requires an explicit acknowledged tree journal')
            actual=document_blocks_snapshot(client,data['document_id'])
            if actual['ids']!=data['block_ids'] or actual['signatures']!=[block_signature(b) for b in payload['document']['children']] or actual['title']!=payload['document']['title']:
                raise NeedsUser('adoption rejected: document differs from acknowledged report')
            observe(client,data['document_id'],expected=data['block_ids'],title=actual['title'])
        period=source['data']['period'];date.fromisoformat(period)
        with store.transaction():
            record=_new_binding(store,key,destination,original['project_id'],actual['title']);binding=record['data']
            binding.update(document_id=data['document_id'],order=[period],sections={period:{'block_ids':list(data['block_ids']),
                'signatures':actual.get('signatures',[]),'report_id':source_id,'report_hash':source['data']['hash'],
                'section_hash':digest(payload['document'])}},ack_revision=actual['revision'],adopted_from=publication_id,
                active_publication=publication_id,member_fingerprint=data.get('document_member_fingerprint'))
            if tree_journal:
                from .document_tree import FORMAT
                section=binding['sections'][period]
                section.update(block_ids=actual['ids'],tree=actual['nodes'])
                binding.update(tree_mode=FORMAT,tree_adoption={'journal':actual['journal_path'],'journal_hash':actual['journal_hash'],
                    'previous_layout_lock':data.get('layout_lock'),'publication_id':publication_id},
                    document_tree={'format':FORMAT,'document_id':data['document_id'],'revision':actual['revision'],'nodes':actual['nodes']})
                data.pop('layout_lock',None)
            data.update(layout='timeline',binding_id=key,binding_record_id=record['id'],adopted_block_ids=list(data['block_ids']),block_ids=[])
            store.update_record(record['id'],binding);store.update_record(publication_id,data)
            store.record('event',{'action':'timeline_adopted','publication_id':publication_id,'binding_id':key,
                'document_id':data['document_id'],'block_ids':binding['sections'][period]['block_ids']},project_id=original['project_id'])
        engine=document_engine(store,client,record)
        try:
            renamed=engine.rename(title) if title is not None else False
        except BaseException as exc:
            engine.fail(exc)
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
            return dict(binding,binding_record_id=record['id'],publication_id=publication_id,message_id=data['message_id'])
        return dict(binding,binding_record_id=record['id'],publication_id=publication_id,message_id=data['message_id'],renamed=renamed)


def rename_timeline(store,settings,ident,title,confirm=False,retry=False,transport=None,base=None,process=None):
    from .feishu import http,BASE
    if not confirm or type(title) is not str or not title.strip() or len(title)>200:
        raise ValueError('rename requires explicit --confirm and a nonempty title up to 200 characters')
    if settings.get('mode')!='app' or settings.get('document_layout','per_period')!='timeline':
        raise ValueError('rename requires configured app timeline layout')
    record=store.get(ident)
    if record['kind']!='doc_binding':raise ValueError('rename requires doc_binding ID')
    client=make_client(settings,transport or http,base or BASE,process)
    with store.claim(['doc:'+record['data']['binding_id']]):
        record=store.get(ident);binding=record['data']
        guard_layout(store, binding)
        if client.destination()!=binding['destination']:raise ValueError('timeline destination mismatch')
        if binding['status'] not in {'completed','retry_authorized','failed'} or binding.get('operation'):
            raise ValueError('unresolved timeline operation; reconcile before rename')
        if binding['status']!='completed' and not retry:raise ValueError('title continuation requires explicit --retry')
        engine=document_engine(store,client,record)
        try:renamed=engine.rename(title)
        except BaseException as exc:
            engine.fail(exc)
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
            return dict(binding,binding_record_id=ident)
        return dict(binding,binding_record_id=ident,renamed=renamed)


def reconcile_timeline(store,settings,ident,resolution=None,reason=None,remote_ids=None,transport=None,base=None,process=None):
    from .feishu import http,BASE
    initial=store.get(ident)
    if initial['kind']=='doc_binding':
        binding_record=initial
        pub_id=initial['data'].get('active_publication')
    elif initial['kind']=='publication' and initial['data'].get('layout')=='timeline':
        binding_record=store.get(initial['data']['binding_record_id']);pub_id=ident
    else:raise ValueError('expected timeline binding/publication')
    if resolution and (resolution not in {'completed','retry_authorized','cancelled'} or type(reason) is not str or not reason.strip()):
        raise ValueError('timeline resolution requires completed/retry_authorized/cancelled and a reason')
    if remote_ids is not None and (not resolution or type(remote_ids) is not dict or set(remote_ids)-{'document_id','message_id','document_member_granted','document_absent','message_absent'}):
        raise ValueError('invalid timeline remote confirmation; explicit resolution/reason required')
    remote_ids=remote_ids or {}
    publication=store.get(pub_id) if pub_id else None
    resources=['doc:'+binding_record['data']['binding_id']]
    if publication:resources.append('publish:'+publication['data']['identity'])
    client=make_client(settings,transport or http,base or BASE,process)
    with store.inspection_lock(resources):
        binding_record=store.get(binding_record['id']);binding=binding_record['data']
        if resolution:guard_layout(store, binding)
        publication=store.get(pub_id) if pub_id else None
        if initial['kind']=='doc_binding' and binding.get('active_publication')!=pub_id:
            raise ValueError('timeline advanced before inspection lock; inspect the current binding again')
        if client.destination()!=binding['destination']:raise ValueError('timeline inspection destination mismatch')
        if binding.get('operation') and pub_id and binding.get('active_publication')!=pub_id:
            raise ValueError('inspect the active timeline operation/binding, not a stale daily publication')
        document_id=remote_ids.get('document_id',binding['document_id'])
        if 'document_id' in remote_ids and (type(document_id) is not str or not document_id):raise ValueError('document_id must be a nonempty string')
        if binding['document_id'] and document_id!=binding['document_id']:raise ValueError('timeline document cannot be silently rebound')
        inspected={};view=None
        if document_id:
            if binding.get('operation'):
                view=operation_view(client,binding)
                inspected['section']={'phase':view['phase'],'revision':view['revision'],'new_ids':view['new_ids'],
                                      'content_hash':digest(view.get('nodes',view.get('signatures')))}
            else:
                view=binding_observe(client,dict(binding,document_id=document_id))
                rename=binding.get('rename_request')
                if view['title'] not in ([rename['old_title'],rename['new_title']] if rename else [binding['title']]):
                    raise NeedsUser('unexpected timeline title; refusing recovery')
                inspected['document']={'document_id':document_id,'revision':view['revision'],'title':view['title'],'block_ids':view['ids']}
        elif not (binding.get('pending') or {}).get('op') in (None,'create_doc'):
            raise NeedsUser('unknown timeline operation has no document ID')
        if publication and (publication['data'].get('message_id') or remote_ids.get('message_id')):
            message_id=remote_ids.get('message_id',publication['data'].get('message_id'))
            if type(message_id) is not str or not message_id:raise ValueError('message_id must be a nonempty string')
            inspected['message']=client.request('GET','/im/v1/messages/'+urllib.parse.quote(message_id,safe=''))
        if not resolution:
            return {'binding_record_id':binding_record['id'],'binding':binding,'publication':publication['data'] if publication else None,
                    'inspection':inspected,'note':'GET only; no writes, resolution, retry or agent resume performed'}
        engine=document_engine(store,client,binding_record)
        pending=binding.get('pending') or {}
        binding_unresolved = binding['status'] != 'completed' or bool(pending) or bool(binding.get('operation')) or bool(binding.get('rename_request'))
        if not document_id and pending.get('op')=='create_doc':
            if resolution!='retry_authorized' or remote_ids.get('document_absent') is not True:
                raise ValueError('unknown document creation needs a verified document_id or explicit document_absent:true with retry_authorized')
        if 'document_absent' in remote_ids and (remote_ids['document_absent'] is not True or document_id or pending.get('op')!='create_doc' or resolution!='retry_authorized'):
            raise ValueError('invalid absent-document confirmation')
        if pending.get('op')=='share' and resolution!='cancelled':
            confirmation=remote_ids.get('document_member_granted')
            if type(confirmation) is not bool or (not confirmation and resolution!='retry_authorized'):
                raise ValueError('unknown sharing needs exact document_member_granted boolean; absent only allows retry_authorized')
            member=client.document_member()
            if not member:raise ValueError('member confirmation requires the original configured member')
            binding['member_fingerprint']=digest([member,client.settings.get('document_member_perm','edit')]) if confirmation else None
            binding['member_verification']='operator_confirmed' if confirmation else 'operator_confirmed_absent'
        elif 'document_member_granted' in remote_ids:
            raise ValueError('no pending timeline member grant to confirm')
        if document_id:binding['document_id']=document_id
        doc_complete=False;section_event=None
        if resolution!='cancelled':
            if binding.get('operation'):
                if resolution=='completed' and view['phase']!='complete':raise ValueError('partial timeline section cannot be completed')
                if initial['kind']=='publication' and resolution=='completed':
                    raise ValueError('resolve document via binding or retry_authorized; card must finish separately')
                op=binding['operation'];op.update(new_ids=view['new_ids'],pending_batch_count=0,phase=view['phase'])
                if op['kind']=='tree':engine.accept_view(view)
                if op['kind']=='patch':op['patched']=view['patched']
                if view['phase']=='complete':
                    section_event=engine.commit_section(view,persist=False);doc_complete=True
                else:binding.update(status='retry_authorized',pending=None)
            elif binding.get('rename_request'):
                rename=binding['rename_request']
                if view['title']==rename['new_title']:
                    binding.update(title=rename['new_title'],rename_request=None,status='completed',pending=None,ack_revision=view['revision']);doc_complete=True
                elif resolution=='completed':raise ValueError('title update did not take effect; cannot mark completed')
                else:binding.update(status='retry_authorized',pending=None)
            else:
                if binding_unresolved:
                    binding.update(status='completed' if resolution=='completed' else 'retry_authorized',pending=None)
                doc_complete=not binding_unresolved or resolution=='completed'
        else:
            if binding_unresolved:
                binding['status']='cancelled'  # Retain pending details/IDs; cancellation is not replay permission.
        if publication:
            data=publication['data']
            if 'message_id' in remote_ids:data['message_id']=remote_ids['message_id']
            unresolved=data['status'] in {'publishing','outcome_unknown','needs_user','failed','cancelled','retry_authorized'}
            if unresolved:
                if resolution=='completed' and not binding.get('operation') and not binding.get('rename_request') and initial['kind']=='publication':
                    if not data.get('message_id'):raise ValueError('completed publication needs a verified message_id')
                    data.update(status='completed',pending=None,outcome_unresolved=False)
                elif resolution=='cancelled':data['status']='cancelled'
                else:
                    card_pending=data.get('pending') or {}
                    if ((card_pending.get('stage')=='create_card' or data.get('outcome_unresolved')) and not data.get('message_id') and
                            remote_ids.get('message_absent') is not True):
                        raise ValueError('unknown card send needs message_id or explicit message_absent:true before retry')
                    data.update(status='retry_authorized',pending=None,outcome_unresolved=False,document_id=binding['document_id'])
            if 'message_absent' in remote_ids and (remote_ids['message_absent'] is not True or data.get('message_id') or resolution!='retry_authorized'):
                raise ValueError('invalid absent-card confirmation')
        with store.transaction():
            engine.save()
            if publication:store.update_record(publication['id'],publication['data'])
            if section_event:store.record('event',section_event,project_id=binding['project_id'])
            for resource in resources:store.db.execute('DELETE FROM locks WHERE resource=?',(resource,))
            store.record('event',{'action':'timeline_resolution','binding_id':binding['binding_id'],'publication_id':pub_id,
                'resolution':resolution,'reason':reason,'remote_ids':remote_ids,'document_complete':doc_complete},project_id=binding['project_id'])
        return {'binding_record_id':binding_record['id'],'binding':binding,'publication':publication['data'] if publication else None,
                'inspection':inspected,'note':'Explicit local resolution only; no remote writes or retry performed'}
