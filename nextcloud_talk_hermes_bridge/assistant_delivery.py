"""A non-admin local assistant account owns delivery copies, never user scratchpads."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from .secure_bridge import Server
from .secure_runner import Handler as BaseHandler
from .assistant_cloud import Cloud
from .assistant_context import context_key

class Delivery:
    def __init__(self,config,state):
        self.config=config;self.state=Path(state);self.pid=None
        self.cloud=Cloud(config['nextcloud_url'],config['assistant_uid'],config['assistant_token'])
        self.registry=self.state/'bindings.json'
        if self.registry.exists():
            bindings=json.loads(self.registry.read_text())
            for tenant in self.config['tenants']:
                tenant['rooms']=list(set(tenant['rooms']+bindings.get(tenant['uid'],[])))

    def bind(self,tenant,room):
        if not isinstance(room,str) or not re.fullmatch('[a-z0-9]{4,30}',room):raise ValueError('Invalid room')
        self.cloud.room(room,tenant['uid'],self.config['assistant_uid'])
        if room not in tenant['rooms']:tenant['rooms'].append(room)
        temporary=self.registry.with_suffix('.partial')
        temporary.write_text(json.dumps({t['uid']:t['rooms'] for t in self.config['tenants']}));temporary.replace(self.registry)
        return {'bound':True}

    def start_thread(self,tenant,p):
        if p.get('room') not in tenant['rooms']:raise PermissionError('Unknown project')
        title=p.get('title','');message=p.get('message','');handoff=p.get('handoff','')
        if not isinstance(title,str) or not 0<len(title)<=100:raise ValueError('Invalid task title')
        if not isinstance(message,str) or not 0<len(message)<=16000:raise ValueError('Invalid handoff')
        if not re.fullmatch('[a-f0-9]{12}',handoff):raise ValueError('Invalid handoff ID')
        self.cloud.room(p['room'],tenant['uid'],self.config['assistant_uid'])
        marker=self.state/('handoff-'+handoff+'.json')
        if marker.exists():
            previous=json.loads(marker.read_text())
            if previous.get('uid')!=tenant['uid'] or previous['room']!=p['room']:raise PermissionError('Wrong handoff owner')
            return previous
        result=self.cloud.ocs('apps/spreed/api/v1/chat/'+p['room'],{'message':message,'threadTitle':title,'referenceId':'handoff-'+handoff},'POST')
        out={'uid':tenant['uid'],'room':p['room'],'thread':int(result['id'])};marker.write_text(json.dumps(out));return out

    def question(self,tenant,p):
        if p.get('room') not in tenant['rooms']:raise PermissionError('Unbound question')
        context_key(tenant['uid'],p['room'],p['thread'])
        if not isinstance(p.get('message'),str) or not 0<len(p['message'])<=6000:raise ValueError('Invalid choice card')
        if not re.fullmatch('[a-f0-9]{12}',p.get('id','')):raise ValueError('Invalid choice ID')
        self.cloud.room(p['room'],tenant['uid'],self.config['assistant_uid'])
        marker=self.state/('question-'+p['id']+'.json')
        if marker.exists():
            previous=json.loads(marker.read_text())
            if previous['uid']!=tenant['uid'] or previous['room']!=p['room']:raise PermissionError('Wrong question owner')
            return previous
        fields={'message':p['message'],'referenceId':'choice-'+p['id']}
        if p['thread']:fields['threadId']=p['thread']
        result=self.cloud.ocs('apps/spreed/api/v1/chat/'+p['room'],fields,'POST')
        out={'uid':tenant['uid'],'room':p['room'],'thread':p['thread'],'message_id':int(result['id'])}
        marker.write_text(json.dumps(out));return out

    def execute(self,p,tenant=None):
        # Tenant comes from a connector-specific bearer, never from the model's chosen UID.
        if tenant is None:raise PermissionError('Missing tenant')
        if p.get('room') not in tenant['rooms']:raise PermissionError('Room is not bound')
        context_key(tenant['uid'],p['room'],p['thread'])
        artifact=p.get('artifact','')
        if not re.fullmatch('[a-f0-9]{12}',artifact):raise ValueError('Invalid artifact')
        name=p.get('name','')
        if not re.fullmatch('[a-zA-Z0-9_.-]{1,100}',name) or name.startswith('.'):raise ValueError('Invalid filename')
        data=base64.b64decode(p.get('data',''),validate=True)
        if not 0<len(data)<=20*1024*1024:raise ValueError('Unsupported size')
        self.cloud.room(p['room'],tenant['uid'],self.config['assistant_uid'])
        user=hashlib.sha256(tenant['uid'].encode()).hexdigest()[:24]
        folder='Deliveries/'+user+'/'+p['room']+'/'+str(p['thread'])+'/'+artifact
        self.cloud.mkdirs(folder);path=folder+'/'+name
        marker=self.state/(artifact+'.json')
        if marker.exists():
            old=json.loads(marker.read_text())
            if old['uid']!=tenant['uid'] or old['room']!=p['room'] or old['thread']!=p['thread']:raise PermissionError('Wrong artifact')
            return old
        self.cloud.upload_file(path,data)
        share=self.cloud.share_file(path,p['room'],p['thread'])
        result={'uid':tenant['uid'],'room':p['room'],'thread':p['thread'],'artifact':artifact,'path':path,'share_id':share['id']}
        marker.write_text(json.dumps(result));return result

class Handler(BaseHandler):
    def do_POST(self):
        if self.path not in ('/deliver','/bind','/start-thread','/question'):self.reply(404,{});return
        tenants=self.server.runner.config['tenants']
        tenant=next((t for t in tenants if hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+t['token'])),None)
        if not tenant:self.reply(401,{});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if self.headers.get('Transfer-Encoding') or not 0<length<=29*1024*1024:self.reply(413,{});return
            data=self.rfile.read(length)
            if len(data)!=length:self.reply(400,{});return
            parsed=json.loads(data)
            if self.path=='/bind':self.reply(200,self.server.runner.bind(tenant,parsed['room']))
            elif self.path=='/start-thread':self.reply(200,self.server.runner.start_thread(tenant,parsed))
            elif self.path=='/question':self.reply(200,self.server.runner.question(tenant,parsed))
            else:self.reply(200,self.server.runner.execute(parsed,tenant))
        except PermissionError:self.reply(403,{'error':'Not authorized'})
        except (ValueError,KeyError,TypeError):self.reply(400,{'error':'Invalid delivery'})
        except Exception as exc:
            print('delivery_error='+type(exc).__name__,flush=True);self.reply(500,{'error':'Delivery failed'})

def main():
    os.umask(0o077)
    cfg=json.loads(Path('/run/config/delivery.json').read_text())
    state=Path('/state');state.mkdir(exist_ok=True)
    server=Server(('0.0.0.0',8792),None,Handler);server.runner=Delivery(cfg,state);server.serve_forever()

if __name__=='__main__':main()
