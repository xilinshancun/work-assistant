"""Tree extension of the existing timeline checkpoint and reconciliation engine."""
import copy
import difflib
import urllib.parse
import uuid
from . import document_tree as tree
from .feishu import APIError, AmbiguousError, TEXT_BLOCKS, block_signature, preview
from .timeline import Document, NeedsUser, section_blocks
from .util import digest, dumps, strict_json


def from_signature(value, bid):
    kind = value['block_type']
    block = {'block_type': kind, TEXT_BLOCKS[kind]: {'elements': [
        {'text_run': {'content': run['content'], 'text_element_style': run['style']}} for run in value['elements']],
        'style': value['style']}}
    return {'id': bid, 'block': block, 'owner': 'tool', 'children': []}


def ensure_tree(binding):
    for section in binding['sections'].values():
        if 'tree' not in section:
            section['tree'] = [from_signature(sig, bid) for sig, bid in zip(section['signatures'], section['block_ids'])]
            if len(section['tree']) != len(section['block_ids']):
                raise NeedsUser('legacy timeline acknowledgement is incomplete')
    binding['tree_mode'] = tree.FORMAT


def shape(block):
    sig = tree.signature(block)
    return dumps(sig if block['block_type'] == 19 else {'block_type': block['block_type'], 'style': sig['style']})


def align(old, target):
    """Keep compatible IDs; preserve explicitly adopted non-source children."""
    if any(n.get('preserved') and any(not later.get('preserved') for later in old[i+1:])
           for i,n in enumerate(old)):
        raise NeedsUser('internal preserved historical nodes require explicit manual review before rewriting')
    eligible = [n for n in old if not n.get('preserved')]
    keys = [shape(n['block']) for n in eligible]; wanted = [shape(b) for b in target]
    for node in eligible:
        if node.get('owner') == 'user-adopted' and (keys.count(shape(node['block'])) != 1 or wanted.count(shape(node['block'])) != 1):
            raise NeedsUser('user-adopted container cannot be removed, restyled or ambiguously matched')
    matcher = difflib.SequenceMatcher(a=keys, b=wanted, autojunk=False)
    matches = {j+k: eligible[i+k] for i,j,length in matcher.get_matching_blocks() for k in range(length)}
    reused = {n['id'] for n in matches.values()}
    for node in eligible:
        if node['id'] not in reused and any(n.get('owner') == 'user-adopted' for n in tree.walk([node])):
            raise NeedsUser('structural replacement would delete a user-adopted subtree')
    result = []
    for i, block in enumerate(target):
        previous = matches.get(i)
        native = {k: copy.deepcopy(v) for k,v in block.items() if k != 'child_blocks'}
        node = {'id': previous['id'] if previous else 'wa_tmp_' + uuid.uuid4().hex,
                'block': native, 'owner': previous.get('owner', 'tool') if previous else 'tool', 'children': []}
        if block['block_type'] == 19:
            node['children'] = align(previous.get('children', []) if previous else [], block['child_blocks'])
        result.append(node)
    result += copy.deepcopy([n for n in old if n.get('preserved')])
    return result


def locate(nodes, bid):
    return next((node for node in tree.walk(nodes) if node['id'] == bid), None)


def children(nodes, parent, document_id):
    if parent == document_id: return nodes
    node = locate(nodes, parent)
    if node is None or node['block']['block_type'] != 19:
        raise NeedsUser('tree operation parent is not an acknowledged container')
    return node['children']


def seed(node, budget):
    value = copy.deepcopy(node); value['children'] = []
    if budget < 1: raise NeedsUser('invalid tree request budget')
    remaining = budget - 1
    for child in node['children']:
        minimum = 2 if child['block']['block_type'] == 19 else 1
        if remaining < minimum: break
        clone = seed(child, remaining); value['children'].append(clone); remaining -= tree.count([clone])
    if node['block']['block_type'] == 19 and not value['children']:
        raise NeedsUser('callout creation requires at least one child')
    return value


def apply_action(working, action, document_id):
    result = copy.deepcopy(working)
    if action['kind'] == 'tree_patch':
        node = locate(result, action['node_id'])
        if node is None: raise NeedsUser('tree patch target disappeared')
        node['block'] = copy.deepcopy(action['block'])
    else:
        values = children(result, action['parent'], document_id); index = action['index']
        if not 0 <= index <= len(values): raise NeedsUser('tree mutation index changed')
        if action['kind'] == 'tree_insert':
            values[index:index] = copy.deepcopy(action['nodes'])
        else:
            old = values[index:index+len(action['ids'])]
            if [n['id'] for n in old] != action['ids'] or any(n.get('owner') == 'user-adopted' for n in tree.walk(old)):
                raise NeedsUser('tree delete would touch unknown or user-adopted nodes')
            del values[index:index+len(action['ids'])]
    return result


def plan_actions(working, desired, parent, document_id, max_blocks):
    """Parent-local inserts (including seeded descendants), patches and deletes."""
    current = copy.deepcopy(working); actions = []; peak = tree.count(current)
    if peak > max_blocks:raise NeedsUser('acknowledged native tree already exceeds local block limit')
    def emit(action):
        nonlocal current, peak
        action['client_token'] = str(uuid.uuid4())
        current = apply_action(current, action, document_id); peak = max(peak, tree.count(current))
        if peak > max_blocks: raise NeedsUser('local tree timeline block limit exceeded, including preserved and temporary nodes')
        actions.append(action)
    def transform(parent_id, target):
        cursor = 0; index = 0
        while index < len(target):
            values = children(current, parent_id, document_id)
            by_id = {n['id']:n for n in values}; wanted = target[index]
            if wanted['id'] in by_id:
                found = by_id[wanted['id']]
                if tree.signature(found['block']) != tree.signature(wanted['block']):
                    if wanted.get('preserved') or wanted['block']['block_type'] == 19:
                        raise NeedsUser('preserved/container style changes require separate explicit adoption')
                    emit({'kind':'tree_patch','node_id':wanted['id'],'block':wanted['block']})
                if wanted['block']['block_type'] == 19:
                    transform(wanted['id'], wanted['children'])
                cursor = next(i for i,n in enumerate(children(current,parent_id,document_id)) if n['id']==wanted['id']) + 1
                index += 1
            else:
                batch = []; budget = 50; start = index
                while index < len(target) and target[index]['id'] not in by_id:
                    node = target[index]; minimum = 2 if node['block']['block_type'] == 19 else 1
                    if budget < minimum: break
                    initial = seed(node, budget); batch.append(initial); budget -= tree.count([initial]); index += 1
                if not batch: raise NeedsUser('native tree insertion cannot fit bounded request')
                emit({'kind':'tree_insert','parent':parent_id,'index':cursor,'nodes':batch})
                for node in target[start:index]:
                    if node['block']['block_type'] == 19: transform(node['id'], node['children'])
                cursor += len(batch)
        wanted_ids = {n['id'] for n in target}
        while True:
            values = children(current,parent_id,document_id)
            first = next((i for i,n in enumerate(values) if n['id'] not in wanted_ids), None)
            if first is None: break
            end = first
            while end < len(values) and values[end]['id'] not in wanted_ids: end += 1
            emit({'kind':'tree_delete','parent':parent_id,'index':first,'ids':[n['id'] for n in values[first:end]]})
    transform(parent, desired)
    return actions, current, peak


def scope_ids(op, working):
    end = len(working) - len(op['suffix'])
    return {n['id'] for n in working[len(op['prefix']):end]}


def check_observation(observed, binding, working, scope=(), templates=()):
    if observed['title'] != binding['title']:
        raise NeedsUser('tree timeline title changed outside explicit rename')
    try: return tree.same_nodes(observed['nodes'], working, scope, templates)
    except APIError as exc: raise NeedsUser(str(exc)) from None


def operation_view(client, binding):
    op = binding['operation']; observed = tree.observe(client,binding['document_id'])
    working = op['working']; pending = binding.get('pending') or {}; phase = 'tree_ready'
    next_index = op['next']; mapping = {}
    if pending.get('op','').startswith('tree_'):
        if pending.get('action_index') != next_index or next_index >= len(op['actions']):
            raise NeedsUser('tree checkpoint action index differs from pending intent')
        action = op['actions'][next_index]
        after = apply_action(working, action, binding['document_id'])
        templates = [n['id'] for n in tree.walk(action.get('nodes', []))]
        try:
            mapping = check_observation(observed,binding,after,scope_ids(op,after),templates)
            if observed['revision'] <= pending['before_revision']:
                raise NeedsUser('tree effect did not advance revision')
            working = remap_nodes(after,mapping); next_index += 1
        except NeedsUser:
            check_observation(observed,binding,working,scope_ids(op,working))
            if observed['revision'] != pending['before_revision']:
                raise NeedsUser('tree effect absent but revision changed; operator inspection required')
    else:
        check_observation(observed,binding,working,scope_ids(op,working))
    if next_index == len(op['actions']): phase = 'complete'
    end = len(working)-len(op['suffix'])
    return dict(observed,phase=phase,working=working,next=next_index,mapping=mapping,
                new_ids=[n['id'] for n in working[len(op['prefix']):end]])


def remap_nodes(nodes, mapping):
    result = copy.deepcopy(nodes)
    for node in tree.walk(result): node['id'] = mapping.get(node['id'],node['id'])
    return result


def map_operation(op, mapping):
    op['target_tree'] = remap_nodes(op['target_tree'],mapping)
    for action in op['actions']:
        for field in ('parent','node_id'):
            if field in action: action[field] = mapping.get(action[field],action[field])
        if 'ids' in action: action['ids'] = [mapping.get(i,i) for i in action['ids']]
        if 'nodes' in action: action['nodes'] = remap_nodes(action['nodes'],mapping)


class TreeDocument(Document):
    def prepare(self, period, report, payload, max_blocks):
        ensure_tree(self.data)
        target = section_blocks(payload,period); old = self.data['sections'].get(period)
        desired = align(old['tree'] if old else [],target)
        full = copy.deepcopy(tree.roots(self.data))
        pos = sum(len(self.data['sections'][day]['block_ids']) for day in self.data['order'] if day>period)
        old_ids = old['block_ids'] if old else []
        prefix = [n['id'] for n in full[:pos]]; suffix = [n['id'] for n in full[pos+len(old_ids):]]
        final = full[:pos] + desired + full[pos+len(old_ids):]
        actions, result, peak = plan_actions(full,final,self.data['document_id'],self.data['document_id'],max_blocks)
        return {'id':str(uuid.uuid4()),'kind':'tree','date':period,'report_id':report['id'],'report_hash':report['data']['hash'],
                'section_hash':digest(target),'prefix':prefix,'suffix':suffix,'old_ids':old_ids,'new_ids':[n['id'] for n in desired],
                'working':full,'source_tree':copy.deepcopy(old['tree']) if old else [],'target_tree':desired,
                'actions':actions,'next':0,'history':[],'peak_blocks':peak}

    def preflight(self, period, report, payload, max_blocks):
        self.prepare(period,report,payload,max_blocks)

    def start_section(self, period, report, payload, max_blocks):
        op = self.prepare(period,report,payload,max_blocks)
        actual = tree.observe(self.client,self.data['document_id'])
        check_observation(actual,self.data,op['working'],scope_ids(op,op['working']))
        op['initial_revision'] = actual['revision']
        self.data.update(operation=op,status='publishing'); self.save()

    def request(self, action, revision):
        doc = urllib.parse.quote(self.data['document_id'],safe='')
        params = urllib.parse.urlencode({'document_revision_id':revision,'client_token':action['client_token']})
        base = '/docx/v1/documents/'+doc+'/blocks/'
        if action['kind']=='tree_patch':
            key = TEXT_BLOCKS[action['block']['block_type']]
            return 'PATCH',base+urllib.parse.quote(action['node_id'],safe='')+'?'+params, {'update_text_elements':{'elements':action['block'][key]['elements']}}
        parent = urllib.parse.quote(action['parent'],safe='')
        if action['kind']=='tree_delete':
            return 'DELETE',base+parent+'/children/batch_delete?'+params, {'start_index':action['index'],'end_index':action['index']+len(action['ids'])}
        if any(n['children'] for n in action['nodes']):
            descendants = []
            for node in tree.walk(action['nodes']):
                native = dict(copy.deepcopy(node['block']),block_id=node['id'])
                if node['children']: native['children'] = [c['id'] for c in node['children']]
                descendants.append(native)
            return 'POST',base+parent+'/descendant?'+params, {'index':action['index'],'children_id':[n['id'] for n in action['nodes']],'descendants':descendants}
        return 'POST',base+parent+'/children?'+params, {'index':action['index'],'children':[copy.deepcopy(n['block']) for n in action['nodes']]}

    def accept_view(self, view):
        op = self.data['operation']; previous = op['next']
        if view['next'] > previous:
            op['history'].append({'action_index':previous,'client_token':op['actions'][previous]['client_token'],
                'mapping':view['mapping'],'revision':view['revision'],'tree_hash':digest(tree.structure(view['working']))})
        map_operation(op,view['mapping'])
        op.update(working=view['working'],next=view['next'],new_ids=view['new_ids'])

    def finish_section(self):
        op = self.data['operation']
        if digest(section_blocks(preview(self.store,op['report_id']),op['date'])) != op['section_hash']:
            raise NeedsUser('immutable native tree report changed')
        if self.data.get('pending'):
            raise NeedsUser('pending tree effect requires explicit reconciliation')
        while op['next'] < len(op['actions']):
            before = operation_view(self.client,self.data)
            action = op['actions'][op['next']]; index = op['next']
            method,path,body = self.request(action,before['revision'])
            response = self.effect(action['kind'],method,path,body,action_index=index,date=op['date'],
                before_revision=before['revision'],client_token=action['client_token'],expected_tree_hash=digest(tree.structure(op['working'])))
            op['response_received'] = copy.deepcopy(response); self.save()
            after = operation_view(self.client,self.data)
            if after['next'] != index+1 or type(response.get('document_revision_id')) is not int or response['document_revision_id'] != after['revision']:
                raise AmbiguousError('tree response and observed effect/revision differ; reconcile without replay')
            if action['kind']=='tree_insert':
                if 'descendants' in body:
                    relations=response.get('block_id_relations')
                    if type(relations) is not list or len(relations)!=len(after['mapping']):
                        raise AmbiguousError('native descendant response missing ID relations')
                    mapping={r.get('temporary_block_id'):r.get('block_id') for r in relations if type(r) is dict}
                else:
                    ids=[n.get('block_id') for n in response.get('children',[]) if type(n) is dict]
                    mapping=dict(zip([n['id'] for n in action['nodes']],ids)) if len(ids)==len(action['nodes']) else {}
                if mapping != after['mapping']:
                    raise AmbiguousError('native insertion response/whole-tree ID mapping differs')
            self.accept_view(after);op.pop('response_received',None);self.ack()
        view=operation_view(self.client,self.data)
        if view['phase']!='complete':raise NeedsUser('native tree section not completely verified; card unchanged')
        self.commit_section(view)

    def commit_section(self, view, persist=True):
        data,op=self.data,self.data['operation']
        data['sections'][op['date']]={'block_ids':[n['id'] for n in op['target_tree']], 'tree':op['target_tree'],
            'signatures':[tree.signature(n['block']) for n in op['target_tree']], 'report_id':op['report_id'],
            'report_hash':op['report_hash'],'section_hash':op['section_hash']}
        data['order']=sorted(data['sections'],reverse=True)
        data.update(status='completed',pending=None,operation=None,ack_revision=view['revision'],tree_mode=tree.FORMAT)
        data['document_tree']={'format':tree.FORMAT,'document_id':data['document_id'],'revision':view['revision'],'nodes':copy.deepcopy(tree.roots(data))}
        data.pop('error',None)
        event={'action':'timeline_tree_section_committed','binding_id':data['binding_id'],'date':op['date'],
            'old_ids':op['old_ids'],'new_ids':view['new_ids'],'report_id':op['report_id'],'operation':op}
        if persist:
            with self.store.transaction():
                self.save();self.store.record('event',event,project_id=data['project_id'])
        return event


def adopt_surgery(store,client,publication,payload,path):
    """Explicit local acknowledgement + live readback; performs no cloud writes."""
    from pathlib import Path
    source=Path(path).expanduser().resolve()
    if source.stat().st_size>16*1024*1024:raise ValueError('tree adoption journal exceeds bound')
    journal=strict_json(source.read_text(encoding='utf-8'));data=publication['data'];ack=data.get('document_tree')
    if (journal.get('status')!='completed' or journal.get('pending') or type(ack) is not dict or
            journal.get('document_id')!=data['document_id'] or journal.get('document_tree')!=ack or
            tree.surgery_hash(ack['snapshot'])!=ack.get('hash')):
        raise NeedsUser('tree adoption needs matching completed surgery journal and stored acknowledgement')
    expected=tree.from_snapshot(ack['snapshot']);actual=tree.observe(client,data['document_id'])
    if expected['title']!=actual['title']:
        raise NeedsUser('tree adoption title differs from acknowledgement')
    try: tree.same_nodes(actual['nodes'],expected['nodes'],{n['id'] for n in expected['nodes']})
    except APIError as exc:raise NeedsUser(str(exc)) from None
    ownership={n['id']:n.get('owner') for n in ack.get('nodes',[])}
    original={b['block_id']:b for b in journal['baseline']['blocks']}
    nodes=expected['nodes']
    for node in tree.walk(nodes):
        owner=ownership.get(node['id'])
        if owner not in {'tool','user-adopted'}:raise NeedsUser('missing/invalid acknowledged node ownership')
        node['owner']=owner
    allowed_users=set()
    for node in tree.walk(nodes):
        if node['owner']=='user-adopted' and node['block']['block_type']==19:
            allowed_users.add(node['id'])
            for bid in original.get(node['id'],{}).get('children',[]):
                child=next((c for c in node['children'] if c['id']==bid),None)
                if child is None or child['block']['block_type']!=2 or child['children'] or any(r['content'].strip() for r in block_signature(child['block'])['elements']):
                    raise NeedsUser('original user child is not an acknowledged empty native text block')
                child.update(owner='user-adopted',preserved=True)
                allowed_users.add(child['id'])
    if any(n['owner']=='user-adopted' and n['id'] not in allowed_users for n in tree.walk(nodes)):
        raise NeedsUser('unsupported initial adopted owner boundary')
    # Do not silently reinterpret a report: leaf payloads, including styles/links,
    # must equal the frozen report after removing layout wrappers/preserved blank.
    def leaves(values):
        result=[]
        for node in values:
            if node.get('preserved'):continue
            if node['block']['block_type']==19:result+=leaves(node['children'])
            else:result.append(block_signature(node['block']))
        return result
    def source_leaves(values):
        result=[]
        for block in values:
            result+=source_leaves(block['child_blocks']) if block['block_type']==19 else [block_signature(block)]
        return result
    if leaves(nodes)!=source_leaves(tree.local_blocks(payload['document'])):
        raise NeedsUser('surgery acknowledgement native content differs from frozen report')
    return dict(actual,nodes=nodes,journal_hash=digest(journal),journal_path=str(source))


def unstarted_history_recovery(store, binding, publication, actual):
    """Classify narrowly bounded historical additions before an absent day's first write.

    Returns a candidate acknowledgement; caller must not save it without an explicit
    retry_authorized resolution. General observation and in-flight recovery stay strict.
    """
    data = publication['data']
    report = store.get(data['report_id'])
    period = report['data']['period']
    if (not binding.get('tree_mode') or binding.get('operation') or binding.get('pending') or
            binding.get('rename_request') or data.get('pending') or data.get('steps') != [] or
            data.get('status') != 'outcome_unknown' or data.get('delivery_scope') != 'document_only' or
            data.get('block_ids') != [] or not binding['sections'] or
            period <= max(binding['sections']) or period in binding['sections'] or
            binding.get('active_publication') not in (None, publication['id']) or
            actual['title'] != binding['title']):
        raise NeedsUser('history additions require an unstarted unknown document-only publication')
    expected = tree.roots(binding)
    known = {n['id'] for n in tree.walk(expected)}
    added = []
    def merge(found, wanted, parent_kind=None):
        if len(found) < len(wanted):
            raise NeedsUser('historical node deleted')
        result = []
        for observed, old in zip(found, wanted):
            if observed['id'] != old['id'] or observed['block']['block_type'] != old['block']['block_type']:
                raise NeedsUser('historical IDs moved or reordered')
            node = copy.deepcopy(old)
            # Keep previous acknowledged payloads for known historical nodes;
            # this operation does not authorize replacing their user edits.
            node['children'] = merge(observed.get('children', []), old.get('children', []), old['block']['block_type'])
            result.append(node)
        for observed in found[len(wanted):]:
            kind = observed['block']['block_type']
            if (observed['id'] in known or observed['id'].startswith('wa_tmp_') or
                    observed.get('children') or observed.get('comments')):
                raise NeedsUser('historical addition is not a new standalone leaf')
            sig = tree.signature(observed['block'])
            if parent_kind is None:
                if kind != 2 or any(r.get('content', '').strip() or 'mention_doc' in r for r in sig['elements']):
                    raise NeedsUser('only empty trailing root text can be preserved')
            elif parent_kind != 19 or kind != 12:
                raise NeedsUser('only trailing historical callout bullets can be preserved')
            node = copy.deepcopy(observed)
            node.update(owner='user-adopted', preserved=True)
            added.append(copy.deepcopy(node))
            result.append(node)
        return result
    # No new date heading may be hidden among otherwise historical content.
    if any(n['block']['block_type'] == 3 and
           ''.join(r.get('content','') for r in tree.signature(n['block'])['elements']).strip() == period
           for n in tree.walk(actual['nodes'])):
        raise NeedsUser('target date already exists remotely')
    # Root additions may be empty text inside one historical day, bounded by
    # known roots of that same day, or empty text after all historical roots.
    root_days = {n['id']: day for day,section in binding['sections'].items() for n in section['tree']}
    expected_roots = {n['id'] for n in expected}
    internal_days = {}
    root_additions = {}
    for i,node in enumerate(actual['nodes']):
        if node['id'] in expected_roots:
            continue
        sig = tree.signature(node['block'])
        if (node['id'] in known or node['id'].startswith('wa_tmp_') or node.get('children') or
                node.get('comments') or node['block']['block_type'] != 2 or
                any(r.get('content','').strip() or 'mention_doc' in r for r in sig['elements'])):
            raise NeedsUser('new root must be an empty standalone text block')
        left = next((n['id'] for n in reversed(actual['nodes'][:i]) if n['id'] in expected_roots), None)
        right = next((n['id'] for n in actual['nodes'][i+1:] if n['id'] in expected_roots), None)
        if right is not None:
            if left not in root_days or right not in root_days or root_days[left] != root_days[right]:
                raise NeedsUser('internal empty root lacks two known anchors in the same historical day')
            internal_days[node['id']] = root_days[left]
        preserved = copy.deepcopy(node)
        preserved.update(owner='user-adopted', preserved=True)
        root_additions[node['id']] = preserved
        added.append(copy.deepcopy(preserved))
    merged_known = merge([n for n in actual['nodes'] if n['id'] in expected_roots], expected)
    by_known = {n['id']: n for n in merged_known}
    merged = [by_known[n['id']] if n['id'] in by_known else root_additions[n['id']] for n in actual['nodes']]
    if not added:
        raise NeedsUser('no explainable historical additions')
    candidate = copy.deepcopy(binding)
    for day,section in candidate['sections'].items():
        section['tree'] = [copy.deepcopy(n) for n in merged
                           if root_days.get(n['id'], internal_days.get(n['id'])) == day]
        if day in internal_days.values():
            section['block_ids'] = [n['id'] for n in section['tree']]
            section['signatures'] = [tree.signature(n['block']) for n in section['tree']]
    assigned = {n['id'] for section in candidate['sections'].values() for n in section['tree']}
    candidate['preserved_tail'] = [copy.deepcopy(n) for n in merged if n['id'] not in assigned]
    candidate['ack_revision'] = actual['revision']
    candidate['document_tree'] = {'format':tree.FORMAT, 'document_id':binding['document_id'],
                                 'revision':actual['revision'], 'nodes':copy.deepcopy(tree.roots(candidate))}
    tree.same_nodes(actual['nodes'], tree.roots(candidate), ())
    return candidate, {'publication_id':publication['id'], 'report_id':report['id'], 'absent_date':period,
        'revision':actual['revision'], 'previous_ack_hash':digest(expected),
        'observed_hash':digest(actual['nodes']), 'added_nodes':added,
        'candidate_ack_hash':digest(tree.roots(candidate)), 'internal_root_dates':internal_days, 'writes_started':False}
