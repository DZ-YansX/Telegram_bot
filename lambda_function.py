"""Function URL ingress and private worker; both handlers share this ZIP."""
import base64
import binascii
import hmac
import json
import logging
import os
import time

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
MAX_BODY = 250000


def response(code, text):
    return {'statusCode': code, 'headers': {'content-type': 'text/plain; charset=utf-8'}, 'body': text}


def allowed_chat(update):
    message = update.get('message') or update.get('callback_query', {}).get('message') or {}
    chat_id = message.get('chat', {}).get('id')
    allowed = {int(v.strip()) for v in os.environ.get('ALLOWED_CHAT_IDS', '').split(',') if v.strip()}
    return chat_id if isinstance(chat_id, int) and chat_id in allowed else None


def lambda_handler(event, context):
    """Only HTTP POST + valid Telegram secret can enqueue work."""
    if not isinstance(event, dict) or event.get('requestContext', {}).get('http', {}).get('method') != 'POST':
        return response(405, 'Method not allowed')
    secret = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')
    if not secret:
        log.error('Webhook secret is not configured')
        return response(503, 'Unavailable')
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    given = headers.get('x-telegram-bot-api-secret-token', '')
    if not isinstance(given, str) or not hmac.compare_digest(given.encode(), secret.encode()):
        return response(403, 'Forbidden')
    try:
        body = event.get('body') or ''
        if not isinstance(body, str) or len(body) > MAX_BODY * 2:
            return response(413, 'Too large')
        raw = base64.b64decode(body, validate=True) if event.get('isBase64Encoded') else body.encode()
        if len(raw) > MAX_BODY:
            return response(413, 'Too large')
        update = json.loads(raw)
        if not isinstance(update, dict) or type(update.get('update_id')) is not int:
            return response(400, 'Invalid update')
        chat_id = allowed_chat(update)
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error):
        return response(400, 'Invalid update')
    if chat_id is None:
        return response(200, 'Ignored')
    try:
        import storage
        if storage.receive(update):
            result = boto3.client('lambda', config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 0})).invoke(
                FunctionName=os.environ['WORKER_FUNCTION_NAME'], InvocationType='Event',
                Payload=json.dumps({'update_id': update['update_id'], 'chat_id': chat_id}).encode())
            if result['StatusCode'] != 202:
                raise RuntimeError('Async invocation was not accepted')
        return response(200, 'OK')
    except Exception as exc:
        log.error('Dispatch failed update=%s type=%s', update['update_id'], type(exc).__name__)
        return response(503, 'Please retry')


async def process(update=None, daily=False):
    import anthropic
    import bot
    from telegram import Update
    app = bot.build_application()
    failed = []

    async def on_error(update, context):
        failed.append(type(context.error).__name__)
        log.error('Handler failed type=%s', failed[-1])

    app.add_error_handler(on_error)
    # No client/HTTP pool crosses event loops; no Application.start, JobQueue,
    # Updater, background polling or unawaited processing tasks.
    async with anthropic.AsyncAnthropic(api_key=os.environ['ANTHROPIC_API_KEY'], timeout=120.0, max_retries=0) as client:
        bot.client = client
        try:
            async with app:
                if daily:
                    await app.bot.send_message(chat_id=int(os.environ['GROUP_CHAT_ID']), text=bot.DAILY_TEXT)
                else:
                    await app.process_update(Update.de_json(update, app.bot))
                if failed:
                    raise RuntimeError('Telegram handler failed')
        finally:
            bot.client = None


def worker_handler(event, context):
    """IAM-only entry. asyncio.run closes its loop and joins to_thread work."""
    import asyncio
    import datetime
    from zoneinfo import ZoneInfo
    import storage
    if not isinstance(event, dict) or 'requestContext' in event:
        raise ValueError('The worker does not accept HTTP events')
    daily = event.get('kind') == 'daily'
    if daily:
        chat_id = int(os.environ['GROUP_CHAT_ID'])
        day = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        update_id = 'daily:' + day
        storage.receive({'update_id': update_id})
    else:
        update_id, chat_id = event['update_id'], event['chat_id']
        if type(update_id) is not int or type(chat_id) is not int:
            raise ValueError('Invalid worker event')
    started = time.monotonic()
    # Acquire before claim: safe automatic retry if another invocation owns chat.
    with storage.chat_lock(chat_id):
        update = storage.claim(update_id)
        if update is None:
            log.info('Duplicate skipped update=%s', update_id)
            return {'duplicate': True}
        try:
            if not daily and allowed_chat(update) != chat_id:
                raise ValueError('Chat mismatch')
            with storage.chat_scope(chat_id):
                # Explicit wall-clock budget leaves time for cleanup and receipts.
                async def bounded():
                    async with asyncio.timeout(270):
                        await process(update, daily=daily)
                asyncio.run(bounded())
            storage.finish(update_id, 'DONE')
            log.info('Processed update=%s duration=%.2fs', update_id, time.monotonic() - started)
            return {'processed': True}
        except BaseException as exc:
            storage.finish(update_id, 'FAILED')
            # Never repeat an uncertain Claude request automatically. A hard
            # Lambda timeout leaves PROCESSING, which also suppresses retries.
            log.error('Processing failed update=%s type=%s', update_id, type(exc).__name__)
            raise
