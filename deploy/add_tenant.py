"""Explicit, restartable native-account provisioning; never grants drive consent."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
from tenant_config import tenant_configs, compose_for_tenants

ROOT = Path('/opt/docker/hermes-talk')


def run(args, input=None):
    p = subprocess.run(args, input=input, capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError('Provisioning command failed (output withheld): ' + args[0])
    return p.stdout


def write(path, value):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value, indent=2))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--uid', required=True)
    parser.add_argument('--slug', required=True)
    parser.add_argument('--display-name', required=True)
    args = parser.parse_args()
    assert re.fullmatch('[a-z][a-z0-9-]{0,31}', args.slug)
    assert re.fullmatch('[a-zA-Z0-9_-]{1,100}', args.uid)
    secret = ROOT / 'secrets'
    info = json.loads(run(['docker', 'exec', '-u', 'www-data', 'nextcloud_app', 'php', 'occ', 'user:info', args.uid, '--output=json']))
    assert info['enabled'] and info['display_name'] == args.display_name
    existing = tenant_configs(ROOT)
    if args.slug in existing:
        assert existing[args.slug]['uid'] == args.uid
        print(json.dumps({'already_registered': True, 'slug': args.slug, 'room': existing[args.slug]['room']}))
        return
    assert args.uid not in [c['uid'] for c in existing.values()]
    with sqlite3.connect(ROOT / 'state-bridge/jobs.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0] == 0, 'Active tasks; retry when idle'
    journal_path = secret / ('tenant-' + args.slug + '.json')
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        assert journal['uid'] == args.uid
    else:
        journal = {'uid': args.uid, 'slug': args.slug, 'display_name': args.display_name,
                   'runner_token': secrets.token_hex(32), 'worker_token': secrets.token_hex(32), 'delivery_token': secrets.token_hex(32)}
        write(journal_path, journal)
    if not journal.get('nextcloud_token'):
        result = run(['docker', 'exec', '-u', 'www-data', 'nextcloud_app', 'php', 'occ', 'user:auth-tokens:add', args.uid,
                      '--name=Hermes Talk - ' + args.display_name + ' - 2026-10-14', '--no-interaction'])
        match = re.search(r'app password:\s*([A-Za-z0-9]{72})', result)
        assert match, 'Unexpected app-token output; inspect privately'
        journal['nextcloud_token'] = match[1]
        write(journal_path, journal)
    # Run native API calls inside the already deployed Connector, with no credential output.
    # List only private rooms to recover a creation interrupted before its journal write.
    payload = {**journal, 'nextcloud_url': existing['ttauth']['nextcloud_url'], 'assistant_uid': existing['ttauth']['assistant_uid']}
    code = '''import json
from nextcloud_talk_hermes_bridge.assistant_cloud import Cloud
p=json.loads(PAYLOAD);n=Cloud(p['nextcloud_url'],p['uid'],p['nextcloud_token'])
identity=n.ocs('cloud/user');assert identity['id']==p['uid']
if p.get('room'):room=p['room']
else:
    rooms=[r for r in n.ocs('apps/spreed/api/v4/room') if r.get('name')=='主助理 · Hermes' and int(r['type'])==2]
    assert len(rooms)<=1,'Ambiguous existing main conversation'
    room=rooms[0]['token'] if rooms else n.ocs('apps/spreed/api/v4/room',{'roomType':2,'roomName':'主助理 · Hermes'},'POST')['token']
people=n.ocs('apps/spreed/api/v4/room/'+room+'/participants')
assert all(x.get('actorType')=='users' and x['actorId'] in (p['uid'],p['assistant_uid']) for x in people)
if not any(x['actorId']==p['assistant_uid'] for x in people):n.ocs('apps/spreed/api/v4/room/'+room+'/participants',{'newParticipant':p['assistant_uid'],'source':'users'},'POST')
n.room(room,p['uid'],p['assistant_uid'])
print(json.dumps({'room':room}))
'''.replace('PAYLOAD', repr(json.dumps(payload)))
    result = json.loads(run(['docker', 'exec', '-i', 'hermes-talk-runner-ttauth', 'python', '-'], code))
    journal['room'] = result['room'];write(journal_path, journal)
    for folder in ['control-' + args.slug, 'workspace-' + args.slug]:
        (ROOT / folder).mkdir(mode=0o700, exist_ok=True)
    baseline = existing['ttauth']
    connector = {k: baseline[k] for k in ('nextcloud_url', 'model_url', 'model_token', 'image_model', 'chat_model', 'allowed_chat_models', 'assistant_uid', 'delivery_url', 'bot_id')}
    connector.update({k: journal[k] for k in ('uid', 'room', 'runner_token', 'worker_token', 'delivery_token', 'nextcloud_token')})
    connector.update({'rooms': [journal['room']], 'project_name': '个人助理', 'worker_url': 'http://hermes-talk-worker-' + args.slug + ':8790'})
    worker = {'uid': args.uid, 'rooms': connector['rooms'], 'runner_token': journal['worker_token'],
              'connector_url': 'http://hermes-talk-runner-' + args.slug + ':8789', 'chat_model': connector['chat_model'], 'allowed_chat_models': connector['allowed_chat_models']}
    bridge = json.loads((secret / 'bridge-v2.json').read_text())
    delivery = json.loads((secret / 'delivery.json').read_text())
    # Preserve a protected rollback snapshot and stop ingress before replacing bindings.
    backup = ROOT / ('before-tenant-' + args.slug + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    for name in ['bridge-v2.json', 'delivery.json']:
        shutil.copy2(secret / name, backup / name)
    for name in ['compose.yaml', '.env']:
        shutil.copy2(ROOT / name, backup / name)
    compose = ['docker', 'compose', '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml')]
    run(compose + ['stop', 'bridge'])
    with sqlite3.connect(ROOT / 'state-bridge/jobs.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0] == 0, 'Task arrived; no binding changes made'
    write(secret / ('connector-' + args.slug + '.json'), connector)
    write(secret / ('worker-' + args.slug + '.json'), worker)
    delivery['tenants'].append({'uid': args.uid, 'rooms': connector['rooms'], 'token': journal['delivery_token']})
    bridge['bindings'].append({'uid': args.uid, 'room': journal['room'], 'runner_url': worker['connector_url'], 'runner_token': journal['runner_token']})
    write(secret / 'delivery.json', delivery);write(secret / 'bridge-v2.json', bridge)
    tenants = tenant_configs(ROOT)
    write(ROOT / 'compose.yaml', compose_for_tenants(json.loads((ROOT / 'compose.personal.yaml').read_text()), tenants))
    # The bot becomes active after the new owner binding is durable. No synthetic user message.
    code = '''import json
from nextcloud_talk_hermes_bridge.assistant_cloud import Cloud
p=json.loads(PAYLOAD);n=Cloud(p['nextcloud_url'],p['uid'],p['nextcloud_token'])
n.ocs('apps/spreed/api/v1/bot/'+p['room']+'/1',{},'POST')
print('bot registered')
'''.replace('PAYLOAD', repr(json.dumps(connector)))
    run(['docker', 'exec', '-i', 'hermes-talk-runner-ttauth', 'python', '-'], code)
    run(compose + ['up', '-d', '--force-recreate', 'delivery'])
    run(compose + ['up', '-d'])
    journal['registered'] = True;write(journal_path, journal)
    print(json.dumps({'registered': True, 'slug': args.slug, 'room': journal['room'], 'cloud_consent': 'unset', 'tenant_count': len(tenants)}))


if __name__ == '__main__':
    main()
