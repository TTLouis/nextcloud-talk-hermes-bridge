"""Publish a real welcome choice without inventing a user request or consent."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from tenant_config import tenant_configs

root = Path('/opt/docker/hermes-talk')
parser = argparse.ArgumentParser();parser.add_argument('--slug', required=True);args = parser.parse_args()
c = tenant_configs(root)[args.slug]
with sqlite3.connect(root / ('control-' + args.slug) / 'assistant.sqlite') as db:
    consent = db.execute('SELECT allowed FROM consent WHERE uid=?', (c['uid'],)).fetchone()
assert consent is None, 'Onboarding is already complete; do not reset it'
job = {'id': hashlib.sha256((c['uid'] + '\0provision-welcome-v1').encode()).hexdigest()[:12],
       'uid': c['uid'], 'room': c['room'], 'thread': 0, 'reply_room': None, 'reply_thread': None}
code = '''import json
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge
from nextcloud_talk_hermes_bridge.assistant_onboarding import FIRST_USE
p=json.loads(PAYLOAD);b=object.__new__(Bridge);b.database='/state/jobs.sqlite'
if not b.has_pending_consent(p['uid']):
    message=FIRST_USE.replace('你的请求已经保留，选择后我会继续处理。','请先点选完成首次设置，然后直接告诉我想做什么。')
    b.queue_choice(p,0,message,{'✅':{'kind':'consent','decision':'allow'},'❌':{'kind':'consent','decision':'deny'}},'consent')
print(json.dumps({'welcome_queued':True}))
'''.replace('PAYLOAD', repr(json.dumps(job)))
p = subprocess.run(['docker', 'exec', '-i', 'hermes-talk-bridge', 'python', '-'], input=code, capture_output=True, text=True, timeout=30)
assert p.returncode == 0, 'Welcome could not be queued (output withheld)'
for _ in range(40):
    with sqlite3.connect(root / 'state-bridge/jobs.sqlite') as db:
        row = db.execute("SELECT message_id FROM choices WHERE uid=? AND room=? AND choice_group='consent' AND status='pending' ORDER BY message_id DESC LIMIT 1", (c['uid'], c['room'])).fetchone()
        sent = db.execute("SELECT status FROM notifications WHERE id=?", (job['id'] + '-0',)).fetchone()
    if row and sent and sent[0] == 'sent':break
    time.sleep(1)
assert row and sent and sent[0] == 'sent', 'Welcome is not yet delivered; inspect notification state'
out = {'slug': args.slug, 'room': c['room'], 'welcome_message_id': row[0], 'cloud_consent': 'unset', 'synthetic_user_request': False}
(root / ('onboarding-' + args.slug + '.json')).write_text(json.dumps(out, indent=2));print(json.dumps(out))
