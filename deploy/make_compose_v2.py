import json
from pathlib import Path

def service(image,name,entry,port,memory,cpus,networks,volumes):
    return {'image':'${'+image+':?Set immutable image ID}','pull_policy':'never','container_name':name,
        'restart':'unless-stopped','init':True,'user':'1000:1000','read_only':True,'cap_drop':['ALL'],
        'security_opt':['no-new-privileges:true'],'mem_limit':memory,'memswap_limit':memory,'cpus':cpus,'pids_limit':96,
        'tmpfs':['/tmp:size=128m,mode=1777'],'volumes':volumes,'networks':networks,
        'healthcheck':{'test':['CMD','python','-c',"import urllib.request;urllib.request.urlopen('http://127.0.0.1:"+str(port)+"/heartbeat',timeout=3).read()"],
            'interval':'30s','timeout':'5s','retries':3},'logging':{'options':{'max-size':'5m','max-file':'2'}}}

services={
 'bridge':service('BRIDGE_IMAGE','hermes-talk-bridge','assistant_bridge',8788,'128m',.5,['nextcloud','jobs'],
    ['./secrets/bridge-v2.json:/run/config/bridge.json:ro','./state-bridge:/state']),
 'runner-ttauth':service('CONNECTOR_IMAGE','hermes-talk-runner-ttauth','assistant_connector',8789,'256m',.5,
    ['nextcloud','jobs','models','worker-ttauth','delivery-calls'],
    ['./secrets/connector-ttauth.json:/run/config/connector.json:ro','./control-ttauth:/state','./workspace-ttauth:/workspace']),
 'worker-ttauth':service('WORKER_IMAGE','hermes-talk-worker-ttauth','assistant_worker',8790,'1g',1,['worker-ttauth'],
    ['./secrets/worker-ttauth.json:/run/config/worker.json:ro','./workspace-ttauth:/workspace']),
 'delivery':service('DELIVERY_IMAGE','hermes-talk-delivery','assistant_delivery',8792,'256m',.5,['nextcloud','delivery-calls'],
    ['./secrets/delivery.json:/run/config/delivery.json:ro','./state-delivery:/state'])}
for name in ['bridge','runner-ttauth','delivery']:services[name]['extra_hosts']=['nextcloud.direct.ttlouis.space:host-gateway']
services['bridge']['depends_on']={'runner-ttauth':{'condition':'service_healthy'}}
services['runner-ttauth']['depends_on']={'worker-ttauth':{'condition':'service_healthy'},'delivery':{'condition':'service_healthy'}}
data={'name':'hermes-talk','services':services,'networks':{
    'jobs':{'internal':True},'worker-ttauth':{'internal':True},'delivery-calls':{'internal':True},
    'nextcloud':{'external':True,'name':'nextcloud_nextcloud_internal'},'models':{'external':True,'name':'ai-gateway-private'}}}
Path(__file__).with_name('compose.personal.yaml').write_text(json.dumps(data,indent=2))
