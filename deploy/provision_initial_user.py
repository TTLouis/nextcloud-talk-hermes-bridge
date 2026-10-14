"""Idempotent server-local provisioning. Never prints any generated credential."""
import json,os,pathlib,re,secrets,subprocess,argparse
root=pathlib.Path('/opt/docker/hermes-talk')
parser=argparse.ArgumentParser(description='Provision the initial, explicitly selected conversation owner');parser.add_argument('--uid',required=True);uid=parser.parse_args().uid
base=['docker','exec','-u','www-data','nextcloud_app','php','occ']
def occ(*args):
    p=subprocess.run(base+list(args),capture_output=True,text=True,timeout=60)
    if p.returncode:
        raise RuntimeError('occ '+args[0]+' failed (credential output withheld)')
    return p.stdout
def write(path,obj):
    path.write_text(json.dumps(obj,indent=2));os.chmod(path,0o600)
secret_dir=root/'secrets';secret_dir.mkdir(mode=0o700,exist_ok=True)
record=secret_dir/'provision.json'
state=json.loads(record.read_text()) if record.exists() else {'uid':uid,'bot_secret':secrets.token_hex(32),'runner_token':secrets.token_hex(32)}
assert state['uid']==uid
write(record,state)
if 'nextcloud_token' not in state:
    name='Hermes Talk - TTauth - 2026-10-14'
    existing=json.loads(occ('user:auth-tokens:list',uid,'--output=json'))
    rows=list(existing.values()) if isinstance(existing,dict) else existing
    assert not any(t.get('name')==name for t in rows),'Named token already exists; reconcile before creating another'
    response=occ('user:auth-tokens:add',uid,'--name='+name,'--no-interaction')
    match=re.search(r'app password:\s*([A-Za-z0-9]{72})',response)
    assert match,'Unexpected native token output'
    state['nextcloud_token']=match.group(1)
    after=json.loads(occ('user:auth-tokens:list',uid,'--output=json'))
    rows=list(after.values()) if isinstance(after,dict) else after
    state['token_id']=[t['id'] for t in rows if t.get('name')==name][0]
    write(record,state)
if 'room' not in state:
    response=occ('talk:room:create','Hermes AI','--owner='+uid,'--user='+uid,
                 '--description=Private image assistant. Temporary images stay in the conversation user folder until explicit save.','--listable=0')
    state['room']=re.search(r'Room token:\s*([A-Za-z0-9]+)',response).group(1)
    write(record,state)
if 'bot_id' not in state:
    response=occ('talk:bot:install','Hermes AI',state['bot_secret'],'http://hermes-talk-bridge:8788/hook',
                 'Private assistant with per-user authorization and explicit save confirmation','--no-setup','--feature=webhook','--feature=response')
    state['bot_id']=int(re.search(r'ID:\s*(\d+)',response).group(1))
    write(record,state)
php=r'''define('OC_CONSOLE',true);require '/var/www/html/lib/base.php';$s=\OCP\Server::get(\OCA\OpenAi\Service\OpenAiSettingsService::class);echo json_encode(['key'=>$s->getAdminApiKey(),'url'=>$s->getServiceUrl()]);'''
model=json.loads(subprocess.run(['docker','exec','-u','www-data','nextcloud_app','php','-r',php],capture_output=True,text=True,check=True).stdout)
runner={'uid':uid,'room':state['room'],'runner_token':state['runner_token'],
        'nextcloud_url':'https://nextcloud.direct.ttlouis.space','nextcloud_token':state['nextcloud_token'],
        'model_url':model['url'],'model_token':model['key'],'chat_model':'gpt-5.6-terra','image_model':'gpt-image-2.5',
        'python':'/opt/hermes/.venv/bin/python'}
bridge={'nextcloud_url':runner['nextcloud_url'],'bot_secret':state['bot_secret'],
        'bindings':[{'uid':uid,'room':state['room'],'runner_url':'http://runner-ttauth:8789','runner_token':state['runner_token']}]}
write(secret_dir/'runner-ttauth.json',runner);write(secret_dir/'bridge.json',bridge)
for name in ['state-bridge','state-ttauth']:
    (root/name).mkdir(mode=0o700,exist_ok=True)
metadata={'uid':uid,'room':state['room'],'bot_id':state['bot_id'],'token_id':state['token_id'],
          'nextcloud_url':runner['nextcloud_url'],'chat_model':runner['chat_model'],'image_model':runner['image_model']}
write(root/'deployment-metadata.json',metadata)
print(json.dumps(metadata))

