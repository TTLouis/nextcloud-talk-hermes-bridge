"""Server-local deployment. Secrets stay under /opt/docker/hermes-talk/secrets."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
root=Path('/opt/docker/hermes-talk');secret=root/'secrets'

def run(args,input=None,timeout=90):
    r=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError('Command failed: '+args[0]+' '+args[1]+' (output withheld)')
    return r.stdout

def occ(*args):return run(['docker','exec','-u','www-data','nextcloud_app','php','occ',*args])
def write(p,v):p.write_text(json.dumps(v,indent=2));os.chmod(p,0o600)

# Check only the existing queue state; never discard in-flight work during upgrade.
import sqlite3
db=sqlite3.connect(root/'state-bridge/jobs.sqlite')
assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]==0,'Existing tasks are active'
db.close()
for name in ['compose.yaml','.env','build-lock.json']:
    backup=root/(name+'.v1')
    if not backup.exists():shutil.copy2(root/name,backup)
old=json.loads((secret/'runner-ttauth.json').read_text());bridge=json.loads((secret/'bridge.json').read_text())
account=secret/'assistant-account.json'
if account.exists():a=json.loads(account.read_text())
else:
    a={'uid':'hermes-assistant','password':secrets.token_urlsafe(64),'worker_token':secrets.token_hex(32),'delivery_token':secrets.token_hex(32)}
    existing=subprocess.run(['docker','exec','-u','www-data','nextcloud_app','php','occ','user:info',a['uid'],'--output=json'],capture_output=True,text=True)
    assert existing.returncode!=0,'An existing account needs explicit reconciliation; will not reset it'
    write(account,a)
if not a.get('created'):
    command='read -r NC_PASS; export NC_PASS; exec php occ user:add --password-from-env --display-name="Hermes 助手" --no-interaction hermes-assistant'
    run(['docker','exec','-i','-u','www-data','nextcloud_app','sh','-c',command],a['password']+'\n')
    a['created']=True;write(account,a)
info=json.loads(occ('user:info',a['uid'],'--output=json'))
assert 'admin' not in info.get('groups',[]),'Assistant must never be admin'
occ('user:setting',a['uid'],'files','quota','1 GB')
if not a.get('app_token'):
    token=occ('user:auth-tokens:add',a['uid'],'--name=Hermes isolated delivery','--no-interaction')
    match=re.search(r'app password:\s*([A-Za-z0-9]{72})',token);assert match,'Unexpected app token output'
    a['app_token']=match[1];write(account,a)
for folder in ['control-ttauth','workspace-ttauth','state-delivery']:(root/folder).mkdir(mode=0o700,exist_ok=True)
rooms=[old['room']]
connector={**old,'rooms':rooms,'assistant_uid':a['uid'],'worker_url':'http://hermes-talk-worker-ttauth:8790',
    'worker_token':a['worker_token'],'delivery_url':'http://hermes-talk-delivery:8792','delivery_token':a['delivery_token'],'bot_id':1,'project_name':'个人助理'}
worker={'uid':old['uid'],'rooms':rooms,'runner_token':a['worker_token'],'connector_url':'http://hermes-talk-runner-ttauth:8789','chat_model':old['chat_model']}
delivery={'assistant_uid':a['uid'],'assistant_token':a['app_token'],'nextcloud_url':old['nextcloud_url'],
    'tenants':[{'uid':old['uid'],'rooms':rooms,'token':a['delivery_token']}]}
write(secret/'connector-ttauth.json',connector);write(secret/'worker-ttauth.json',worker);write(secret/'delivery.json',delivery)
bridge['assistant_uid']=a['uid'];write(secret/'bridge-v2.json',bridge)
lock=json.loads((root/'build-lock.json').read_text());images={}
for target in ['bridge','connector','worker','delivery']:
    subprocess.run(['docker','build','--target',target,'--build-arg','PYTHON_BASE='+lock['python_base'],
        '-f','Dockerfile.secure','-t','local/hermes-talk-'+target+':personal-2','.'],cwd=root,check=True,timeout=900)
    data=json.loads(run(['docker','image','inspect','local/hermes-talk-'+target+':personal-2']))[0]
    images[target]={'id':data['Id'],'size':data['Size']}
tests=subprocess.run(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp:size=32m,mode=1777',
    '-v',str(root/'tests/secure')+':/tests:ro','--entrypoint','python',images['bridge']['id'],'-m','unittest','discover','-s','/tests','-q'],capture_output=True,text=True,timeout=45)
assert tests.returncode==0,tests.stderr
write(root/'contract-tests.json',{'image':images['bridge']['id'],'returncode':tests.returncode,'report':tests.stderr.strip()})
print(json.dumps({'tests':tests.stderr.strip(),'assistant_non_admin':True,'assistant_password_entropy_bits':512}),flush=True)
compose=['docker','compose','--env-file',str(root/'.env'),'-f',str(root/'compose.yaml')]
db=sqlite3.connect(root/'state-bridge/jobs.sqlite')
assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]==0,'A task arrived during the build; leave current services running'
db.close()
run(compose+['stop','bridge','runner-ttauth'])
# Native supported state change enables setup for new project rooms. Ingress still denies every unregistered UID/room.
occ('talk:bot:state','1','1','--feature=webhook','--feature=response','--feature=reaction')
# Invite the delivery-only account; do not invite any family member.
code='''import json,pathlib
from nextcloud_talk_hermes_bridge.assistant_cloud import Cloud
c=json.loads(pathlib.Path('/run/config/connector.json').read_text());n=Cloud(c['nextcloud_url'],c['uid'],c['nextcloud_token'])
people=n.ocs('apps/spreed/api/v4/room/'+c['room']+'/participants')
if not any(x.get('actorId')==c['assistant_uid'] for x in people):n.ocs('apps/spreed/api/v4/room/'+c['room']+'/participants',{'newParticipant':c['assistant_uid'],'source':'users'},'POST')
print('private assistant participant ready')
'''
run(['docker','run','--rm','-i','--network','nextcloud_nextcloud_internal','--add-host','nextcloud.direct.ttlouis.space:host-gateway',
    '-v',str(secret/'connector-ttauth.json')+':/run/config/connector.json:ro','--entrypoint','python',images['connector']['id'],'-'],code)
env=''.join(k.upper()+'_IMAGE='+v['id']+'\n' for k,v in images.items())
(root/'.env').write_text(env);os.chmod(root/'.env',0o600)
shutil.copy2(root/'compose.personal.yaml',root/'compose.yaml')
run(compose+['up','-d'],timeout=120)
lock['personal_images']=images;lock['mode']='personal-assistant-v2';write(root/'build-lock.json',lock)
consent_db=sqlite3.connect(root/'control-ttauth/assistant.sqlite')
consent_row=consent_db.execute('SELECT allowed FROM consent WHERE uid=?',(old['uid'],)).fetchone();consent_db.close()
print(json.dumps({'deployed':True,'images':images,'cloud_read_consent':'unset' if not consent_row else 'granted' if consent_row[0] else 'declined'}))
