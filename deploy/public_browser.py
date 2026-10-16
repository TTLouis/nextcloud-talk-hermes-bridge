"""Configure isolated public browsing for registered Connectors; no cloud scopes."""
import argparse,datetime,json,os,pathlib,re,secrets,shutil,sqlite3,subprocess

def write(path,value):
    temp=path.with_suffix('.pending');temp.write_text(json.dumps(value,indent=2));temp.chmod(0o600);temp.replace(path)

def run(args,**kwargs):
    p=subprocess.run(args,capture_output=True,text=True,**kwargs)
    if p.returncode:raise RuntimeError(p.stderr[-3000:])
    return p.stdout

def enable(root):
    import sys
    sys.path.insert(0,str(root/'deploy'))
    from tenant_config import tenant_configs
    tenants=tenant_configs(root);folder=root/'web-browser';folder.mkdir(exist_ok=True)
    with sqlite3.connect(root/'state-bridge/jobs.sqlite') as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE status IN ('running','queued')").fetchone(),'Active work; try when idle'
    marker=folder/'enable-backup-path.txt'
    if marker.exists():backup=pathlib.Path(marker.read_text());assert backup.is_dir() and backup.parent==root
    else:
        backup=root/('before-public-web-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir(mode=0o700)
        shutil.copy2(root/'compose.yaml',backup/'compose.yaml')
        for slug in tenants:shutil.copy2(root/'secrets'/('connector-'+slug+'.json'),backup/('connector-'+slug+'.json'))
        marker.write_text(str(backup));marker.chmod(0o600)
    tokens=set();browser=[]
    for slug,c in tenants.items():
        token=c.get('browser_token') or secrets.token_hex(32)
        assert re.fullmatch('[a-f0-9]{64}',token) and token not in tokens
        tokens.add(token);c.update({'browser_url':'http://hermes-talk-browser:9120','browser_token':token})
        write(root/'secrets'/('connector-'+slug+'.json'),c)
        browser.append({'uid':c['uid'],'token':token})
    write(folder/'browser.json',{'tenants':browser})
    compose=json.loads((root/'compose.yaml').read_text())
    compose['networks']['web-calls']={'external':True,'name':'hermes-talk-web-calls'}
    for slug in tenants:
        networks=compose['services']['runner-'+slug]['networks']
        if 'web-calls' not in networks:networks.append('web-calls')
    write(root/'compose.yaml',compose)
    # Sidecar has no provider or Nextcloud credential and no user workspace mount.
    image=json.loads((folder/'build.json').read_text())['image']
    python=json.loads((root/'build-lock.json').read_text())['personal_images']['bridge']['id']
    base={'restart':'unless-stopped','init':True,'user':'1000:1000','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'logging':{'options':{'max-size':'3m','max-file':'2'}}}
    sidecar={'services':{
        'browser':{**base,'image':image,'container_name':'hermes-talk-browser','mem_limit':'768m','memswap_limit':'768m','cpus':1,'pids_limit':256,'tmpfs':['/tmp:size=384m,mode=1777'],'shm_size':'128m',
            'volumes':[str(folder/'browser.json')+':/run/config/browser.json:ro'],'networks':['calls','browser-egress'],
            'healthcheck':{'test':['CMD','node','-e',"require('http').get('http://127.0.0.1:9120/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"],'interval':'10s','timeout':'4s','retries':3}},
        'egress':{**base,'image':python,'container_name':'hermes-talk-web-egress','entrypoint':['python','/app/egress_proxy.py'],'mem_limit':'96m','memswap_limit':'96m','cpus':0.25,'pids_limit':64,'tmpfs':['/tmp:size=16m,mode=1777'],
            'volumes':[str(folder/'egress_proxy.py')+':/app/egress_proxy.py:ro'],'networks':['browser-egress','public']}},
        'networks':{'calls':{'name':'hermes-talk-web-calls','internal':True},'browser-egress':{'internal':True},'public':{}}}
    write(folder/'compose.yaml',sidecar)
    # Atomic config replacement changes its inode; recreate to reload bearer map.
    run(['docker','compose','-p','hermes-talk-web','-f',str(folder/'compose.yaml'),'up','-d','--force-recreate'],timeout=120)
    print(json.dumps({'configured_registered_users':list(tenants),'new_cloud_permissions':False,'worker_network_changes':False,'core_restart_required':True}))

def build(root):
    source=pathlib.Path(__file__).resolve().parent.parent/'browser';folder=root/'web-browser';folder.mkdir(exist_ok=True)
    for name in ('package.json','package-lock.json','Dockerfile','.dockerignore','browser_service.mjs','egress_proxy.py'):
        shutil.copy2(source/name,folder/name)
    node='node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
    run(['docker','build','--memory','2g','--build-arg','NODE_BASE='+node,'-t','local/hermes-talk-browser:public-web','.'],cwd=folder,timeout=900)
    item=json.loads(run(['docker','image','inspect','local/hermes-talk-browser:public-web']))[0]
    write(folder/'build.json',{'image':item['Id'],'size':item['Size'],'node_base':node,'playwright_cli':'0.1.22'})
    print(json.dumps({'browser_image':item['Id']}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=pathlib.Path,default=pathlib.Path('/opt/docker/hermes-talk'));parser.add_argument('--build',action='store_true');parser.add_argument('--enable',action='store_true');args=parser.parse_args()
    os.umask(0o077)
    if args.build:build(args.root)
    if args.enable:enable(args.root)
