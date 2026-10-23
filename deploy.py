"""Build/deploy/manage the bot. Credentials come from environment or AWS login.

Never put keys in this file. Run AWS operations in AWS CloudShell if the local
machine has no AWS credentials. `deploy` provisions; `webhook` cuts over.
"""
import argparse
import asyncio
import base64
import gzip
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

NAME = 'telegram-claude'
REGION = os.environ.get('AWS_REGION', 'ap-southeast-1')
ROOT = Path(__file__).resolve().parent


def build():
    with tempfile.TemporaryDirectory(prefix='telegram-lambda-build-') as folder:
        target = Path(folder)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', str(target),
                        '--platform', 'manylinux_2_28_aarch64', '--platform', 'manylinux2014_aarch64', '--python-version', '3.12',
                        '--implementation', 'cp', '--abi', 'cp312', '--only-binary=:all:',
                        '--no-compile', '-r', str(ROOT / 'requirements.txt')], check=True)
        for name in ['bot.py', 'storage.py', 'lambda_function.py']:
            shutil.copy2(ROOT / name, target / name)
        output = ROOT / 'build' / 'lambda.zip'
        output.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(target.rglob('*')):
                if file.is_file() and '__pycache__' not in file.parts and not file.name.endswith('.pyc'):
                    archive.write(file, file.relative_to(target))
        print(f'Created {output} ({output.stat().st_size / 1024 / 1024:.1f} MB)')


def aws(service):
    import boto3
    from botocore.config import Config
    return boto3.client(service, region_name=REGION, config=Config(
        proxies={} if os.environ.get('AWS_DIRECT') == '1' else None,
        connect_timeout=10, read_timeout=120, retries={'mode': 'standard', 'max_attempts': 3}))


def tolerate(call, codes, **kwargs):
    from botocore.exceptions import ClientError
    try:
        return call(**kwargs)
    except ClientError as exc:
        if exc.response['Error']['Code'] not in codes:
            raise
        return None


def role(name, service, statements):
    iam = aws('iam')
    account = aws('sts').get_caller_identity()['Account']
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}]}
    if service == 'scheduler.amazonaws.com':
        trust['Statement'][0]['Condition'] = {'StringEquals': {'aws:SourceAccount': account}, 'ArnLike': {'aws:SourceArn': f'arn:aws:scheduler:{REGION}:{account}:schedule-group/default'}}
    created = tolerate(iam.create_role, ['EntityAlreadyExists'], RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName=name, PolicyName=NAME, PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': statements}))
    return (created or iam.get_role(RoleName=name))['Role']['Arn']


def config(path):
    env = json.loads(Path(path).read_text())
    for key in ['TELEGRAM_BOT_TOKEN', 'ANTHROPIC_API_KEY', 'ALLOWED_CHAT_IDS']:
        if not env.get(key):
            raise ValueError(f'Missing {key} in private environment file')
    if not env.get('TELEGRAM_WEBHOOK_SECRET'):
        env['TELEGRAM_WEBHOOK_SECRET'] = secrets.token_urlsafe(32)
        Path(path).write_text(json.dumps(env))
        Path(path).chmod(0o600)
    return {k: str(v) for k, v in env.items()}


def deploy(args):
    env = config(args.env_file)
    account = aws('sts').get_caller_identity()['Account']
    ddb, lam, logs = aws('dynamodb'), aws('lambda'), aws('logs')
    limits = lam.get_account_settings()['AccountLimit']
    can_reserve = limits['ConcurrentExecutions'] >= 103
    table_name = NAME + '-state'
    table_arn = f'arn:aws:dynamodb:{REGION}:{account}:table/{table_name}'
    worker = NAME + '-worker'
    worker_arn = f'arn:aws:lambda:{REGION}:{account}:function:{worker}'
    ingress = NAME + '-webhook'
    tolerate(ddb.create_table, ['ResourceInUseException'], TableName=table_name,
             AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ['pk', 'sk']],
             KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
             BillingMode='PROVISIONED', ProvisionedThroughput={'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5},
             TableClass='STANDARD', Tags=[{'Key': 'Project', 'Value': NAME}])
    ddb.get_waiter('table_exists').wait(TableName=table_name)
    if ddb.describe_time_to_live(TableName=table_name)['TimeToLiveDescription']['TimeToLiveStatus'] == 'DISABLED':
        ddb.update_time_to_live(TableName=table_name, TimeToLiveSpecification={'Enabled': True, 'AttributeName': 'expires_at'})
    def logging_policy(function):
        return {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],
                'Resource': f'arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{function}:*'}
    table_policy = {'Effect': 'Allow', 'Action': ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'], 'Resource': table_arn}
    receipt_policy = {'Effect': 'Allow', 'Action': ['dynamodb:GetItem', 'dynamodb:PutItem'], 'Resource': table_arn,
                      'Condition': {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['UPDATE#*']}}}
    ingress_role = role(ingress, 'lambda.amazonaws.com', [logging_policy(ingress), receipt_policy,
                        {'Effect': 'Allow', 'Action': 'lambda:InvokeFunction', 'Resource': worker_arn}])
    worker_role = role(worker, 'lambda.amazonaws.com', [logging_policy(worker), table_policy])
    code = ({'S3Bucket': args.s3_bucket, 'S3Key': args.s3_key} if args.s3_bucket
            else {'ZipFile': Path(args.zip).read_bytes()})
    for function, handler, memory, timeout, role_arn, variables in [
        (worker, 'lambda_function.worker_handler', 512, 300, worker_role,
         {k: v for k, v in env.items() if k != 'TELEGRAM_WEBHOOK_SECRET'} | {'DYNAMODB_TABLE': table_name}),
        (ingress, 'lambda_function.lambda_handler', 256, 15, ingress_role,
         {k: env[k] for k in ['TELEGRAM_WEBHOOK_SECRET', 'ALLOWED_CHAT_IDS']} | {'DYNAMODB_TABLE': table_name, 'WORKER_FUNCTION_NAME': worker})]:
        tolerate(logs.create_log_group, ['ResourceAlreadyExistsException'], logGroupName='/aws/lambda/' + function)
        logs.put_retention_policy(logGroupName='/aws/lambda/' + function, retentionInDays=7)
        kwargs = dict(FunctionName=function, Runtime='python3.12', Role=role_arn, Handler=handler,
                      Timeout=timeout, MemorySize=memory, Environment={'Variables': variables},
                      Description='Personal Telegram Claude bot', EphemeralStorage={'Size': 512})
        existing = tolerate(lam.get_function, ['ResourceNotFoundException'], FunctionName=function)
        if existing:
            lam.update_function_code(FunctionName=function, **code, Architectures=['arm64'])
            lam.get_waiter('function_updated_v2').wait(FunctionName=function)
            lam.update_function_configuration(**kwargs)
            lam.get_waiter('function_updated_v2').wait(FunctionName=function)
        else:
            from botocore.exceptions import ClientError
            for attempt in range(12):
                try:
                    lam.create_function(**kwargs, Code=code, Architectures=['arm64'], PackageType='Zip')
                    break
                except ClientError as exc:
                    if exc.response['Error']['Code'] != 'InvalidParameterValueException' or 'role' not in str(exc).lower():
                        raise
                    time.sleep(5)
            else:
                raise RuntimeError('IAM role propagation timed out')
            lam.get_waiter('function_active_v2').wait(FunctionName=function)
        if can_reserve:
            lam.put_function_concurrency(FunctionName=function, ReservedConcurrentExecutions=1 if function == worker else 2)
        else:
            print('Reservation unavailable; shared account concurrency limit:', limits['ConcurrentExecutions'], '(DynamoDB chat lock remains active)')
        print('Ready:', function, memory, 'MB', timeout, 'seconds')
    lam.put_function_event_invoke_config(FunctionName=worker, MaximumEventAgeInSeconds=3600, MaximumRetryAttempts=2)
    result = tolerate(lam.create_function_url_config, ['ResourceConflictException'], FunctionName=ingress, AuthType='NONE', InvokeMode='BUFFERED')
    if not result:
        result = lam.get_function_url_config(FunctionName=ingress)
        if result['AuthType'] != 'NONE':
            raise RuntimeError('Existing function URL must be reviewed before changing authentication')
    for permission in [
        {'StatementId': 'TelegramFunctionUrl', 'Action': 'lambda:InvokeFunctionUrl', 'FunctionUrlAuthType': 'NONE'},
        {'StatementId': 'TelegramUrlOnlyInvoke', 'Action': 'lambda:InvokeFunction', 'InvokedViaFunctionUrl': True}]:
        tolerate(lam.add_permission, ['ResourceConflictException'], FunctionName=ingress, Principal='*', **permission)
    scheduler_role = role(NAME + '-schedule', 'scheduler.amazonaws.com',
                          [{'Effect': 'Allow', 'Action': 'lambda:InvokeFunction', 'Resource': worker_arn}])
    # Preserve the existing 17:30–19:30 Shanghai window without a sleeping process.
    schedule = dict(Name=NAME + '-daily', ScheduleExpression='cron(30 17 * * ? *)',
                    ScheduleExpressionTimezone='Asia/Shanghai', FlexibleTimeWindow={'Mode': 'FLEXIBLE', 'MaximumWindowInMinutes': 120},
                    State='DISABLED', Target={'Arn': worker_arn, 'RoleArn': scheduler_role,
                    'Input': json.dumps({'kind': 'daily'}), 'RetryPolicy': {'MaximumEventAgeInSeconds': 3600, 'MaximumRetryAttempts': 2}})
    scheduler = aws('scheduler')
    from botocore.exceptions import ClientError
    for attempt in range(12):
        try:
            created = tolerate(scheduler.create_schedule, ['ConflictException'], **schedule)
            break
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ValidationException' or 'assume the role' not in str(exc):
                raise
            time.sleep(5)
    else:
        raise RuntimeError('Scheduler IAM role propagation timed out')
    if not created:
        schedule['State'] = scheduler.get_schedule(Name=schedule['Name'])['State']
        scheduler.update_schedule(**schedule)
    deployment = {'region': REGION, 'function_url': result['FunctionUrl'], 'worker': worker, 'webhook': ingress, 'table': table_name, 'schedule': schedule['Name'],
                  'reserved_concurrency': can_reserve, 'account_concurrency': limits['ConcurrentExecutions']}
    (ROOT / 'deployment.json').write_text(json.dumps(deployment, indent=2))
    print(json.dumps(deployment, indent=2))
    print('Webhook and daily schedule are not enabled yet. Import history and stop polling first.')


def transfer_db(args, export=False):
    import boto3
    from botocore.config import Config
    table = boto3.resource('dynamodb', region_name=REGION, config=Config(
        proxies={} if os.environ.get('AWS_DIRECT') == '1' else None)).Table(NAME + '-state')
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    if export:
        db.executescript("CREATE TABLE IF NOT EXISTS memory(chat_id INTEGER PRIMARY KEY,content TEXT NOT NULL); CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY,model TEXT NOT NULL); CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,content_json TEXT);")
        result = table.scan(FilterExpression='sk = :state', ExpressionAttributeValues={':state': 'STATE'})
        items = result['Items']
        while result.get('LastEvaluatedKey'):
            result = table.scan(FilterExpression='sk = :state', ExpressionAttributeValues={':state': 'STATE'}, ExclusiveStartKey=result['LastEvaluatedKey'])
            items.extend(result['Items'])
        for item in items:
            chat_id = int(item['pk'].split('#')[1])
            state = json.loads(gzip.decompress(bytes(item['data'])))
            db.execute('INSERT OR REPLACE INTO memory VALUES(?,?)', (chat_id, state['memory']))
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (chat_id, state['model']))
            db.execute('DELETE FROM messages WHERE chat_id=?', (chat_id,))
            for m in state['messages']:
                # New bot.py can restore Telegram references in SQLite too.
                rich = json.dumps(m['visual'], ensure_ascii=False) if m.get('visual') else None
                db.execute('INSERT INTO messages(chat_id,role,content,content_json) VALUES(?,?,?,?)', (chat_id,m['role'],m['content'],rich))
        db.commit()
        print('Exported chats:', len(items))
    else:
        ids = {r[0] for r in db.execute('SELECT chat_id FROM messages UNION SELECT chat_id FROM settings UNION SELECT chat_id FROM memory')}
        for chat_id in sorted(ids):
            memory = db.execute('SELECT content FROM memory WHERE chat_id=?',(chat_id,)).fetchone()
            model = db.execute('SELECT model FROM settings WHERE chat_id=?',(chat_id,)).fetchone()
            rows = db.execute('SELECT role,content,content_json FROM messages WHERE chat_id=? ORDER BY id',(chat_id,)).fetchall()
            messages = []
            for row in rows:
                m = {'role':row['role'],'content':row['content']}
                if row['content_json']:
                    m['visual'] = json.loads(row['content_json'])
                messages.append(m)
            state = {'memory': memory[0] if memory else '', 'model': model[0] if model else '', 'messages': messages}
            import storage
            item = {'pk':f'CHAT#{chat_id}','sk':'STATE','data':storage.encode(state),'updated_at':int(time.time())}
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
            roundtrip = table.get_item(Key={'pk':item['pk'],'sk':'STATE'},ConsistentRead=True)['Item']
            assert json.loads(gzip.decompress(bytes(roundtrip['data']))) == state
            print('Imported and verified chat:',chat_id,'messages:',len(messages))
    db.close()


async def telegram(args):
    from telegram import Bot
    env = config(args.env_file)
    async with Bot(env['TELEGRAM_BOT_TOKEN']) as client:
        if args.command == 'webhook':
            url = args.url or json.loads((ROOT / 'deployment.json').read_text())['function_url']
            await client.set_webhook(url=url, secret_token=env['TELEGRAM_WEBHOOK_SECRET'], max_connections=1,
                                     allowed_updates=['message','callback_query'], drop_pending_updates=False)
            os.environ.update(env)
            import bot
            from types import SimpleNamespace
            await bot.post_init(SimpleNamespace(bot=client))
        elif args.command == 'delete-webhook':
            await client.delete_webhook(drop_pending_updates=False)
        info = await client.get_webhook_info()
        # Only non-sensitive configuration/health fields.
        print(json.dumps({k: v for k, v in info.to_dict().items() if k in ['url','pending_update_count','last_error_message','max_connections','allowed_updates','last_error_date']},default=str,indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['build','deploy','import-db','export-db','webhook','delete-webhook','status'])
    parser.add_argument('--env-file', default='secrets.json')
    parser.add_argument('--zip', default=str(ROOT/'build/lambda.zip'))
    parser.add_argument('--s3-bucket', help='Optional temporary staging bucket if direct ZIP upload is unreliable')
    parser.add_argument('--s3-key', default='lambda.zip')
    parser.add_argument('--db', default='bot.db')
    parser.add_argument('--url')
    args = parser.parse_args()
    if args.command == 'build': build()
    elif args.command == 'deploy': deploy(args)
    elif args.command in ['import-db','export-db']: transfer_db(args, export=args.command=='export-db')
    else: asyncio.run(telegram(args))


if __name__ == '__main__':
    main()
