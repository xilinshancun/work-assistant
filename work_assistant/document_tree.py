"""Native callout trees and bounded, stable whole-document observations."""
import copy
import urllib.parse
from .feishu import APIError, TEXT_BLOCKS, block_signature
from .util import digest

FORMAT = 'wa.document.tree.v1'


def callout_signature(value):
    if type(value) is not dict or set(value) - {'background_color', 'border_color', 'text_color', 'emoji_id'}:
        raise APIError('unsupported native callout fields')
    value = copy.deepcopy(value)
    for field in ('background_color', 'border_color', 'text_color'):
        if field in value and (type(value[field]) is not int or not 0 <= value[field] <= 20):
            raise APIError('invalid native callout color')
    if 'emoji_id' in value and (type(value['emoji_id']) is not str or not value['emoji_id'] or len(value['emoji_id']) > 100):
        raise APIError('invalid native callout emoji')
    if value.get('text_color') == 0:
        value.pop('text_color')
    return value


def signature(block):
    if type(block) is dict and type(block.get('block_type')) is int and block['block_type'] == 19:
        return {'block_type': 19, 'callout': callout_signature(block.get('callout'))}
    return block_signature(block)


def local_blocks(document):
    """Internal child_blocks are objects; API children always contain IDs."""
    if type(document) is not dict or type(document.get('title')) is not str or not document['title'].strip():
        raise ValueError('invalid document title')
    if document.get('format') == FORMAT:
        if set(document) != {'format', 'title', 'blocks'}:
            raise ValueError('invalid tree document fields')
        blocks = document['blocks']
    elif 'format' not in document:
        if set(document) != {'title', 'children'}:
            raise ValueError('invalid flat document fields')
        blocks = document['children']
    else:
        raise ValueError('unsupported document format')
    count = [0]
    def validate(values, depth=0):
        if type(values) is not list or not values or depth > 1:
            raise ValueError('native tree requires nonempty blocks and at most one callout layer')
        for block in values:
            if type(block) is not dict:
                raise ValueError('invalid native tree block')
            count[0] += 1
            if count[0] > 1000:
                raise ValueError('native tree exceeds local report block bound')
            if type(block.get('block_type')) is int and block['block_type'] == 19:
                if set(block) != {'block_type', 'callout', 'child_blocks'} or depth:
                    raise ValueError('callout needs internal child_blocks, without remote IDs or nesting')
                signature(block); validate(block['child_blocks'], depth + 1)
            else:
                kind = block.get('block_type')
                if type(kind) is not int or kind not in TEXT_BLOCKS or set(block) != {'block_type', TEXT_BLOCKS[kind]}:
                    raise ValueError('unsupported native tree leaf')
                block_signature(block)
    validate(blocks)
    return copy.deepcopy(blocks)


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get('children', []))


def count(nodes):
    return sum(1 for _ in walk(nodes))


def roots(binding):
    return [node for day in binding['order'] for node in binding['sections'][day]['tree']]


def payload(node):
    block = copy.deepcopy(node['block'])
    if node['block']['block_type'] == 19:
        block['child_blocks'] = [payload(child) for child in node['children'] if not child.get('preserved')]
    return block


def structure(nodes):
    return [(n['id'], n['block']['block_type'], structure(n.get('children', []))) for n in nodes]


def same_nodes(actual, expected, scope, templates=()):
    """Full structure; signatures/comments only inside the selected day section.

    Historical edits are observed but never acknowledged as replacement authority.
    Unknown new IDs may be inferred only for this exact pending request's templates.
    """
    templates = set(templates); mapped = {}; used = set()
    expected_ids = {n['id'] for n in walk(expected)}
    def compare(found, wanted, checked=False):
        if len(found) != len(wanted): raise APIError('native tree child count differs from acknowledgement')
        for actual_node, node in zip(found, wanted):
            bid, aid = node['id'], actual_node['id']
            if aid in used: raise APIError('duplicate native tree ID')
            used.add(aid)
            if bid in templates:
                if aid in expected_ids or bid in mapped:
                    raise APIError('new native tree ID collides with known IDs')
                mapped[bid] = aid
            elif aid != bid:
                raise APIError('native tree IDs/order changed')
            if actual_node['block']['block_type'] != node['block']['block_type']:
                raise APIError('native tree block type changed')
            check = checked or bid in scope
            if check and (signature(actual_node['block']) != signature(node['block']) or actual_node.get('comments')):
                raise APIError('target native tree contains user edits, styles or comments')
            compare(actual_node.get('children', []), node.get('children', []), check)
    compare(actual, expected)
    return mapped


def observe(client, document_id, limit=10000):
    doc = urllib.parse.quote(document_id, safe=''); path = '/docx/v1/documents/' + doc
    before = client.request('GET', path).get('document', {})
    revision = before.get('revision_id')
    if type(revision) is not int or revision < 1 or before.get('document_id') != document_id:
        raise APIError('native tree document metadata unavailable')
    blocks, token, seen = [], None, set()
    for _ in range(200):
        params = {'page_size': '500', 'document_revision_id': '-1'}
        if token: params['page_token'] = token
        value = client.request('GET', path + '/blocks?' + urllib.parse.urlencode(params))
        if type(value.get('items')) is not list or type(value.get('has_more')) is not bool:
            raise APIError('invalid whole-tree pagination')
        blocks += value['items']
        if len(blocks) > limit + 1: raise APIError('whole native tree inspection bound exceeded')
        if not value['has_more']: break
        token = value.get('page_token')
        if type(token) is not str or not token or token in seen:
            raise APIError('invalid whole-tree page token')
        seen.add(token)
    else: raise APIError('whole native tree pagination incomplete')
    after = client.request('GET', path).get('document', {})
    if after != before: raise APIError('native tree changed during observation')
    return from_snapshot({'before': {'document': before}, 'after': {'document': after}, 'blocks': blocks})


def from_snapshot(snapshot):
    before, after = snapshot['before']['document'], snapshot['after']['document']
    if before != after or type(after.get('revision_id')) is not int:
        raise APIError('native tree snapshot has unstable metadata')
    document_id = after['document_id']; lookup = {}; visited = set()
    for block in snapshot['blocks']:
        bid = block.get('block_id')
        if type(bid) is not str or not bid or bid in lookup:
            raise APIError('missing or duplicate native tree ID')
        lookup[bid] = block
    root = lookup.get(document_id)
    if not root or root.get('block_type') != 1 or root.get('parent_id', '') != '':
        raise APIError('native document root unavailable')
    visited.add(document_id)
    def children(parent, depth=0):
        if depth > 2: raise APIError('unsupported native tree depth')
        ids = lookup[parent].get('children', [])
        if type(ids) is not list: raise APIError('invalid native child IDs')
        result = []
        for bid in ids:
            if type(bid) is not str or bid in visited or bid not in lookup:
                raise APIError('native tree cycle, duplicate edge or missing child')
            visited.add(bid); block = lookup[bid]; kind = block.get('block_type')
            if block.get('parent_id') != parent:
                raise APIError('native tree parent differs from child list')
            if type(kind) is not int or kind not in set(TEXT_BLOCKS) | {19}:
                raise APIError('unsupported native tree node')
            field = 'callout' if kind == 19 else TEXT_BLOCKS[kind]
            native = {'block_type': kind, field: copy.deepcopy(block[field])}
            signature(native)
            if kind != 19 and block.get('children'):
                raise APIError('native leaf has unsupported children')
            comments = block.get('comment_ids', [])
            if type(comments) is not list: raise APIError('invalid native comments')
            result.append({'id': bid, 'block': native, 'children': children(bid, depth + 1), 'comments': copy.deepcopy(comments), 'owner': 'tool'})
        return result
    nodes = children(document_id)
    if set(lookup) != visited: raise APIError('unexpected orphan native blocks')
    return {'document_id': document_id, 'title': after['title'], 'revision': after['revision_id'], 'nodes': nodes,
            'ids': [n['id'] for n in nodes], 'snapshot': snapshot}


def surgery_hash(snapshot):
    """Compatibility with the already-reviewed private surgery acknowledgement."""
    observation = from_snapshot(snapshot); table = {b['block_id']: b for b in snapshot['blocks']}
    rows = []
    def emit(bid, parent):
        b = table[bid]; kind = b['block_type']
        sig = block_signature({'block_type': 2, 'text': b['page']}) if kind == 1 else (
            callout_signature(b['callout']) if kind == 19 else block_signature(b))
        rows.append({'id': bid, 'parent': parent, 'type': kind, 'children': b.get('children', []), 'signature': sig})
        for child in b.get('children', []): emit(child, bid)
    emit(observation['document_id'], '')
    return digest({'document_id': observation['document_id'], 'title': observation['title'], 'nodes': rows})
