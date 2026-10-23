"""One DynamoDB table: chat state, short-lived update receipts and chat locks.

Only the active invocation has an in-memory working copy. DynamoDB is the source
of truth; every invocation reloads it. No SQLite/files are used by Lambda.
"""
import contextlib
import contextvars
import copy
import gzip
import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

_active = contextvars.ContextVar('chat_state')
MAX_HISTORY = 40  # Also bounds growth when Claude memory compression fails.
MAX_CONTEXT_CHARS = 96000
MAX_STATE_BYTES = 350000


def table():
    return boto3.resource('dynamodb').Table(os.environ['DYNAMODB_TABLE'])


def conditional_failure(exc):
    return isinstance(exc, ClientError) and exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


def key(chat_id):
    return {'pk': f'CHAT#{chat_id}', 'sk': 'STATE'}


def encode(state):
    data = gzip.compress(json.dumps(state, ensure_ascii=False).encode(), mtime=0)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError('Chat state exceeds DynamoDB item budget')
    return data


def load(chat_id):
    item = table().get_item(Key=key(chat_id), ConsistentRead=True).get('Item')
    return json.loads(gzip.decompress(bytes(item['data']))) if item else {'memory': '', 'messages': [], 'model': ''}


@contextlib.contextmanager
def chat_scope(chat_id):
    token = _active.set((chat_id, load(chat_id)))
    try:
        yield
        flush()
    finally:
        _active.reset(token)


def state(chat_id):
    current_id, data = _active.get()
    if current_id != chat_id:
        raise RuntimeError('Mismatched chat scope')
    return data


def flush():
    chat_id, data = _active.get()
    table().put_item(Item={**key(chat_id), 'data': encode(data), 'updated_at': int(time.time())})


def get_model(chat_id):
    from bot import DEFAULT_MODEL, MODELS, MODEL_UPGRADES
    model = state(chat_id)['model']
    model = MODEL_UPGRADES.get(model, model)
    return model if model in {m[1] for m in MODELS.values()} else DEFAULT_MODEL


def set_model(chat_id, model):
    state(chat_id)['model'] = model


def get_thinking(chat_id):
    return bool(state(chat_id).get('thinking'))


def set_thinking(chat_id, enabled):
    state(chat_id)['thinking'] = bool(enabled)


def get_memory(chat_id):
    return state(chat_id)['memory']


def set_memory(chat_id, content):
    state(chat_id)['memory'] = '\n'.join(content.splitlines()[:200])[:32000]


def add_message(chat_id, role, content, rich_content=None):
    messages = state(chat_id)['messages']
    entry = {'role': role, 'content': content}
    if getattr(rich_content, 'file_id', None):
        entry['visual'] = {'telegram_file_id': rich_content.file_id, 'text': content}
    elif isinstance(rich_content, list):
        entry['visual'] = rich_content
    messages.append(entry)
    for entry in messages[:-6]:
        entry.pop('visual', None)
    # Preserve complete user/assistant pairs if repeated compression fails.
    while len(messages) > MAX_HISTORY or (len(messages) > 6 and sum(len(m['content']) for m in messages) > MAX_CONTEXT_CHARS):
        del messages[:2]


def get_messages(chat_id, include_images=True):
    messages = state(chat_id)['messages']
    return [{'role': m['role'], 'content': copy.deepcopy(m.get('visual', m['content']) if include_images else m['content'])} for m in messages]


def count_messages(chat_id):
    return len(state(chat_id)['messages'])


def trim_to_recent(chat_id, keep):
    data = state(chat_id)
    data['messages'] = data['messages'][-keep:] if keep else []


def clear_all(chat_id):
    data = state(chat_id)
    data['messages'], data['memory'] = [], ''


def receipt_key(update_id):
    return {'pk': f'UPDATE#{update_id}', 'sk': 'RECEIPT'}


def receive(update):
    """Persist before dispatch. RECEIVED may be dispatched again safely."""
    k = receipt_key(update['update_id'])
    item = {**k, 'status': 'RECEIVED', 'payload': json.dumps(update, separators=(',', ':')),
            'expires_at': int(time.time()) + 7 * 86400}
    try:
        table().put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
        return True
    except ClientError as exc:
        if not conditional_failure(exc):
            raise
        old = table().get_item(Key=k, ConsistentRead=True).get('Item', {})
        return old.get('status') == 'RECEIVED'


def claim(update_id):
    try:
        result = table().update_item(
            Key=receipt_key(update_id),
            UpdateExpression='SET #s = :processing, started_at = :now',
            ConditionExpression='#s = :received',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':processing': 'PROCESSING', ':received': 'RECEIVED', ':now': int(time.time())},
            ReturnValues='ALL_NEW')
        return json.loads(result['Attributes']['payload'])
    except ClientError as exc:
        if not conditional_failure(exc):
            raise
        return None


def finish(update_id, status):
    table().update_item(Key=receipt_key(update_id), UpdateExpression='SET #s = :s REMOVE payload',
                        ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':s': status})


@contextlib.contextmanager
def chat_lock(chat_id):
    """Defence in depth for manual invocations / accidental concurrency changes."""
    k = {'pk': f'CHAT#{chat_id}', 'sk': 'LOCK'}
    owner, now = uuid.uuid4().hex, int(time.time())
    table().put_item(Item={**k, 'owner': owner, 'expires_at': now + 360},
                     ConditionExpression='attribute_not_exists(pk) OR expires_at < :now',
                     ExpressionAttributeValues={':now': now})
    try:
        yield
    finally:
        table().delete_item(Key=k, ConditionExpression='#o = :owner',
                            ExpressionAttributeNames={'#o': 'owner'}, ExpressionAttributeValues={':owner': owner})
