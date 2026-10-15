"""One-user authority: consent, cloud reads, explicit saves, model proxy, task grants."""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import struct
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from .secure_bridge import Server
from .secure_runner import Handler as BaseHandler
from .secure_common import request,json_request
from .assistant_cloud import Cloud
from .assistant_context import context_key,task_workspace,read_workspace,write_workspace,relative_path
from .assistant_onboarding import FIRST_USE
from .assistant_titles import conversation_title
from .assistant_models import ModelSettings
from .assistant_outputs import ReadableOutputs,readable_filename
from .assistant_replies import ReplyDetails

class Connector(ReplyDetails,ReadableOutputs,ModelSettings):
    def __init__(self,config,state,workspace):
        self.config=config;self.state=Path(state);self.workspace=Path(workspace)
        self.state.mkdir(parents=True,exist_ok=True);self.workspace.mkdir(parents=True,exist_ok=True)
        (self.state/'artifacts').mkdir(exist_ok=True)
        self.nc=Cloud(config['nextcloud_url'],config['uid'],config['nextcloud_token'])
        self.lock=threading.Lock();self.grant_lock=threading.Lock();self.active=None;self.pid=None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS consent (uid TEXT PRIMARY KEY, allowed INTEGER NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS projects (room TEXT PRIMARY KEY,name TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY,room TEXT,thread INTEGER,name TEXT,status TEXT,sha256 TEXT,saved_path TEXT,delivery TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY,room TEXT,thread INTEGER,data TEXT,status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS task_state (room TEXT,thread INTEGER,status TEXT,summary TEXT,updated REAL,PRIMARY KEY(room,thread))')
            db.execute('CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY,source_room TEXT,source_thread INTEGER,target_room TEXT,target_thread INTEGER,prompt TEXT,title TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY,room TEXT,thread INTEGER,question TEXT,options TEXT,status TEXT,answer TEXT)')
            for r in config['rooms']:
                db.execute('INSERT OR IGNORE INTO projects VALUES (?,?)',(r,conversation_title(config.get('project_name','Hermes'),main=r==config['room'])))
        self.init_models()
        self.init_outputs()
        self.init_replies()

    @contextmanager
    def db(self):
        c=sqlite3.connect(self.state/'assistant.sqlite',timeout=10);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def projects(self):
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM projects')]

    def authorize(self,p):
        if p.get('uid')!=self.config['uid'] or p.get('room') not in [r['room'] for r in self.projects()]:
            raise PermissionError('Identity/project mismatch')
        if not re.fullmatch('[a-f0-9]{12}',p.get('job_id','')):raise ValueError('Invalid job')
        if type(p.get('thread')) is not int or p['thread']<0:raise ValueError('Invalid thread')
        return self.nc.room(p['room'],self.config['uid'],self.config['assistant_uid'])

    def tasks(self,room):
        data=self.nc.ocs('apps/spreed/api/v1/chat/'+room+'/threads/recent')
        return [{'id':r['thread']['id'],'name':r['thread'].get('title') or r['thread'].get('name','')} for r in data]

    def consent(self):
        with self.db() as db:
            r=db.execute('SELECT allowed FROM consent WHERE uid=?',(self.config['uid'],)).fetchone()
        return None if r is None else bool(r['allowed'])

    def execute(self,p):
        room_info=self.authorize(p)
        if not self.lock.acquire(False):raise BlockingIOError('Connector busy')
        try:
            kind=p['kind']
            is_answer=kind=='answer';question_origin=None
            if kind=='model_menu':return self.model_menu(p)
            if kind in ('model_scope','model_select'):return self.model_decision(p)
            if kind=='answer':
                with self.db() as db:
                    q=db.execute('SELECT * FROM questions WHERE id=? AND room=? AND thread=?',(p['artifact'],p['room'],p['thread'])).fetchone()
                    if not q or q['status']!='pending' or p.get('decision') not in json.loads(q['options']):raise PermissionError('No matching pending question')
                    db.execute("UPDATE questions SET status='answered',answer=? WHERE id=?",(p['decision'],q['id']))
                    origin=db.execute('SELECT origin_job FROM question_models WHERE question_id=?',(q['id'],)).fetchone()
                    question_origin=origin['origin_job'] if origin else None
                p={**p,'kind':'task','prompt':'用户通过 reaction 对问题「'+q['question']+'」选择了「'+p['decision']+'」。请在本任务继续处理。此选择只回答该问题，不是网盘读取或保存权限的授权。'}
                kind='task'
            if kind=='status':
                tasks=self.tasks(p['room'])
                message='当前项目的任务：\n'+'\n'.join('• '+t['name']+'（线程 '+str(t['id'])+'）' for t in tasks)
                with self.db() as db:
                    pending=db.execute("SELECT id,name,thread FROM artifacts WHERE room=? AND status='pending'",(p['room'],)).fetchall()
                if pending:message+='\n\n待保存成果（请在对应线程回复）：\n'+'\n'.join(r['name']+'：保存 '+r['id']+'（线程 '+str(r['thread'])+'）' for r in pending)
                return {'message':message}
            if kind=='consent':
                allowed=p.get('decision')=='allow'
                with self.db() as db:db.execute('INSERT OR REPLACE INTO consent VALUES (?,?,?)',(self.config['uid'],int(allowed),time.time()))
                return {'onboarding_complete':True,'message':'已允许通过 MCP 按任务读取你本人可访问的网盘文件。可随时发送「撤销网盘授权」关闭。' if allowed else '已关闭网盘读取。仍可对话、使用私有工作区和生图。'}
            if kind in ('save','discard'):return self.decide(p)
            if kind=='details':return self.view_details(p)
            if kind=='confirm':return self.confirm(p)
            if kind=='cancel':
                with self.db() as db:
                    changed=db.execute("UPDATE proposals SET status='cancelled' WHERE id=? AND room=? AND thread=? AND status='pending'",(p['artifact'],p['room'],p['thread'])).rowcount
                if not changed:raise PermissionError('No matching pending proposal')
                return {'message':'已取消这项修改。'}
            if kind=='create_project':return self.project_change({'kind':'create','name':p['prompt']},p)
            if kind!='task':raise ValueError('Invalid action')
            if self.consent() is None:
                return {'requires_consent':True,'message':FIRST_USE}
            # Native room-scoped thread lookup validates that the task actually belongs to this project.
            thread_info=self.nc.ocs('apps/spreed/api/v1/chat/'+p['room']+'/threads/'+str(p['thread'])) if p['thread'] else {}
            p={**p,'project_name':room_info.get('displayName','') if isinstance(room_info,dict) else '',
               'task_name':(thread_info.get('thread',{}).get('title') or thread_info.get('thread',{}).get('name','')) if p['thread'] else '主助理' if p['room']==self.config['room'] else '项目协调',
               'coordination':p['thread']==0}
            model=self.assign_task_model(p,question_origin,is_answer)
            p={**p,'chat_model':model['model'],'task_root':model['root_job']}
            work=task_workspace(self.workspace,self.config['uid'],p['room'],p['thread'])
            token=secrets.token_hex(32)
            with self.grant_lock:self.active={'token':token,'payload':dict(p),'work':work,'expires':time.monotonic()+660,'tools':0,'models':0,'artifacts':[],'proposals':[],'handoffs':[],'questions':[]}
            with self.db() as db:db.execute('INSERT OR REPLACE INTO task_state VALUES (?,?,?,?,?)',(p['room'],p['thread'],'running','',time.time()))
            try:
                _,result=json_request(self.config['worker_url']+'/execute',method='POST',timeout=640,limit=65536,
                    headers={'Authorization':'Bearer '+self.config['worker_token']},body={**p,'grant':token})
                self.retain_reply(p,work,result)
                result['model']=model['model'];result['model_source']=model['source']
                with self.grant_lock:
                    result['artifacts']=list(self.active['artifacts']);result['proposals']=list(self.active['proposals']);result['handoffs']=list(self.active['handoffs']);result['questions']=list(self.active['questions'])
                with self.db() as db:db.execute('INSERT OR REPLACE INTO task_state VALUES (?,?,?,?,?)',(p['room'],p['thread'],'done',result.get('message','')[:2000],time.time()))
                return result
            except Exception as exc:
                with self.db() as db:db.execute('INSERT OR REPLACE INTO task_state VALUES (?,?,?,?,?)',(p['room'],p['thread'],'failed',type(exc).__name__,time.time()))
                raise
            finally:
                with self.grant_lock:self.active=None
        finally:self.lock.release()

    def delivery_request(self,*args,**kwargs):
        return json_request(*args,**kwargs)

    def active_grant(self,bearer,count=False,model=False):
        with self.grant_lock:
            a=self.active
            if not a or time.monotonic()>a['expires'] or not hmac.compare_digest(bearer,'Bearer '+a['token']):
                raise PermissionError('Expired or foreign task grant')
            if count:
                a['tools']+=1
                if a['tools']>40:raise ValueError('Tool request budget exceeded')
            if model:
                a['models']+=1
                if a['models']>32:raise ValueError('Model request budget exceeded')
            return a

    def work_relative(self,a,path):
        relative_path(path)
        return str(a['work'].relative_to(self.workspace))+'/scratch/'+path

    def tool(self,a,name,args):
        p=a['payload']
        self.nc.room(p['room'],self.config['uid'],self.config['assistant_uid'])
        if name=='assistant_projects':
            with self.db() as db:states=[dict(r) for r in db.execute('SELECT * FROM task_state')]
            return {'projects':self.projects(),'task_progress':states,'contains_raw_chat_history':False}
        if name=='handoff_project':return self.handoff(a,args)
        if name=='ask_user':
            question=args.get('question','');options=args.get('options',[])
            if not isinstance(question,str) or not 0<len(question)<=2000:raise ValueError('Invalid question')
            if not isinstance(options,list) or not 2<=len(options)<=5 or len(set(options))!=len(options) or any(not isinstance(s,str) or not 0<len(s)<=120 for s in options):raise ValueError('Invalid options')
            ident=hashlib.sha256((p['job_id']+'\0question').encode()).hexdigest()[:12]
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO questions VALUES (?,?,?,?,?,?,?)',(ident,p['room'],p['thread'],question,json.dumps(options),'pending',None))
                db.execute('INSERT OR IGNORE INTO question_models VALUES (?,?)',(ident,p['job_id']))
            out={'id':ident,'question':question,'options':options}
            with self.grant_lock:
                if self.active['questions'] and self.active['questions'][0]!=out:raise ValueError('One choice question at a time')
                if out not in self.active['questions']:self.active['questions'].append(out)
            return {'awaiting_user_choice':True,'question_id':ident,'instruction':'End this turn. Host will present emoji choices and resume this task.'}
        if name=='project_tasks':return {'tasks':self.tasks(p['room']),'includes_other_task_history':False}
        if name.startswith('nextcloud_'):
            if self.consent() is not True:raise PermissionError('User has not consented to cloud reads')
            path=args.get('path','')
            if name=='nextcloud_list':return {'entries':self.nc.list(path)}
            if name=='nextcloud_read_text':return {'path':path,'untrusted_content':self.nc.download(path,256*1024).decode('utf-8')}
            if name=='nextcloud_download':
                data=self.nc.download(path)
                name_on_disk=secrets.token_hex(4)+'-'+Path(path).name
                safe=re.sub('[^a-zA-Z0-9_.-]','_',name_on_disk)[:110]
                dest='inbox/'+safe
                write_workspace(self.workspace,self.work_relative(a,dest),data)
                return {'local_path':str(a['work']/'scratch'/dest),'relative_path':dest,'bytes':len(data),'untrusted_file':True}
            raise ValueError('Unsupported cloud tool')
        if name=='generate_image':
            prompt=args.get('prompt','')
            if not isinstance(prompt,str) or not 0<len(prompt)<=8000:raise ValueError('Invalid prompt')
            # Durable claim BEFORE provider request: no repeated billing on retries/restarts.
            claim=self.state/('image-'+p['job_id']+'.claimed')
            with claim.open('x') as handle:handle.write(p['job_id'])
            _,response=json_request(self.config['model_url']+'/images/generations',method='POST',timeout=240,limit=40*1024*1024,
                headers={'Authorization':'Bearer '+self.config['model_token']},
                body={'model':self.config['image_model'],'prompt':prompt,'n':1,'size':'1024x1024'})
            data=base64.b64decode(response['data'][0]['b64_json'],validate=True)
            if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)>20*1024*1024:raise ValueError('Invalid image')
            dest='outputs/'+p['job_id']+'.png';write_workspace(self.workspace,self.work_relative(a,dest),data)
            width,height=struct.unpack('>II',data[16:24])
            return {'path':dest,'bytes':len(data),'width':width,'height':height,'saved_to_user_drive':False}
        if name=='present_file':
            path=args['path'];data=read_workspace(self.workspace,self.work_relative(a,path))
            if not data:raise ValueError('Empty output')
            artifact=hashlib.sha256((p['job_id']+'\0'+path).encode()).hexdigest()[:12]
            filename=readable_filename(path,args.get('name'))
            self.prepare_snapshot(p,artifact,filename,data)
            result=self.deliver_snapshot(p,artifact)
            with self.grant_lock:
                if result not in self.active['artifacts']:self.active['artifacts'].append(result)
            return {**result,'delivered_as_assistant':True,'saved_to_user_drive':False}
        if name=='propose_project_change':
            if args.get('kind') not in ('create','rename','avatar','emoji'):raise ValueError('Invalid project change')
            data=dict(args)
            if data['kind']=='avatar':
                binary=read_workspace(self.workspace,self.work_relative(a,data['path']),4*1024*1024)
                if not binary.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('PNG required')
                width,height=struct.unpack('>II',binary[16:24])
                if width!=height:raise ValueError('Avatar requires square PNG; crop a copy inside the task workspace first')
                data={'kind':'avatar','png':base64.b64encode(binary).decode()}
            ident=secrets.token_hex(6)
            with self.db() as db:db.execute('INSERT INTO proposals VALUES (?,?,?,?,?)',(ident,p['room'],p['thread'],json.dumps(data),'pending'))
            label={'create':'新建项目对话','rename':'修改当前项目名称','avatar':'更换当前项目图标','emoji':'更换当前项目表情图标'}[data['kind']]
            proposal={'id':ident,'description':label+('：'+data.get('name','') if data.get('name') else '')}
            with self.grant_lock:self.active['proposals'].append(proposal)
            return {**proposal,'requires_user_confirmation':True}
        raise ValueError('Unknown tool')

    def handoff(self,a,args):
        p=a['payload']
        if p['thread']!=0:raise PermissionError('Dedicated task agents must finish their task; handoff is a coordinator capability')
        with self.grant_lock:
            if self.active['handoffs']:raise ValueError('At most one handoff per main-assistant turn')
        for field,limit in [('name',100),('task_title',100),('goal',2000),('context',10000)]:
            if not isinstance(args.get(field),str) or not 0<len(args[field])<=limit:raise ValueError('Invalid handoff '+field)
        for field,limit in [('next_steps',12),('cloud_files',12),('scratch_files',8)]:
            values=args.get(field,[])
            if not isinstance(values,list) or len(values)>limit or any(not isinstance(v,str) or len(v)>1024 for v in values):raise ValueError('Invalid handoff references')
        ident=hashlib.sha256((p['job_id']+'\0handoff').encode()).hexdigest()[:12]
        note=('目标：'+args['goal']+'\n\n相关背景与已做决定：\n'+args['context']+'\n\n下一步：\n'+
              '\n'.join('- '+s for s in args['next_steps'])+'\n\n网盘文件引用（仍需 MCP 读取授权）：\n'+
              '\n'.join(args.get('cloud_files',[])))
        if len(note)>14000:raise ValueError('Handoff summary too long; condense relevant context and reference large files')
        with self.db() as db:previous=db.execute('SELECT * FROM handoffs WHERE id=?',(ident,)).fetchone()
        if previous:
            out=dict(previous)
            with self.grant_lock:self.active['handoffs'].append(out)
            return {'handoff_scheduled':True,'project_room':out['target_room'],'thread':out['target_thread']}
        for path in args.get('scratch_files',[]):read_workspace(self.workspace,self.work_relative(a,path))
        # Fail closed after an uncertain external outcome; don't create duplicate projects on tool retries.
        with (self.state/('handoff-'+ident+'.claimed')).open('x') as claim:claim.write(p['job_id'])
        room=args.get('project_room')
        if room:
            if room==self.config['room'] or room not in [r['room'] for r in self.projects()]:raise PermissionError('Not a dedicated bound project')
            self.nc.room(room,self.config['uid'],self.config['assistant_uid'])
        else:
            created=self.project_change({'kind':'create','name':args['name']},p);room=created['new_project']
        _,target=json_request(self.config['delivery_url']+'/start-thread',method='POST',timeout=60,limit=65536,
            headers={'Authorization':'Bearer '+self.config['delivery_token']},
            body={'room':room,'title':args['task_title'],'handoff':ident,'message':'主助理交接\n\n'+note})
        thread=int(target['thread']);destination=task_workspace(self.workspace,self.config['uid'],room,thread)
        relative=str(destination.relative_to(self.workspace))+'/scratch/'
        write_workspace(self.workspace,relative+'HANDOFF.md',note.encode())
        copied=[]
        for index,path in enumerate(args.get('scratch_files',[])):
            data=read_workspace(self.workspace,self.work_relative(a,path))
            basename=re.sub('[^a-zA-Z0-9_.-]','_',Path(path).name)[:90].lstrip('.') or 'file'
            dest='handoff-files/'+str(index)+'-'+basename
            write_workspace(self.workspace,relative+dest,data);copied.append(dest)
        prompt='【主助理 handoff】你在独立项目任务中接收以下相关上下文，请据此继续执行下一步。原主对话仍保留，完成后宿主会回报主助理。不要再创建项目或重新交接。\n'+note+'\n已复制到本任务工作区的必要文件：'+json.dumps(copied,ensure_ascii=False)
        out={'id':ident,'source_room':p['room'],'source_thread':p['thread'],'target_room':room,'target_thread':thread,'prompt':prompt,'title':args['task_title']}
        with self.db() as db:db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?,?,?)',tuple(out[k] for k in ['id','source_room','source_thread','target_room','target_thread','prompt','title']))
        with self.grant_lock:self.active['handoffs'].append(out)
        return {'handoff_scheduled':True,'project_room':room,'task_thread':thread,'project_url':self.config['nextcloud_url']+'/call/'+room,
            'source_conversation_preserved':True,'instruction':'Host will execute this project task. Briefly notify the user; do not duplicate its work in the coordinator.'}

    def decide(self,p):
        ident=p['artifact']
        with self.db() as db:r=db.execute('SELECT * FROM artifacts WHERE id=? AND room=? AND thread=?',(ident,p['room'],p['thread'])).fetchone()
        if not r:raise PermissionError('Output belongs to another task')
        if p['kind']=='discard':
            if r['status']=='saved':return {'message':'这份成果此前已保存。取消保存选择不会删除已经保存的文件。'}
            if r['status'] not in ('pending','discarded'):raise ValueError('Output is not awaiting a save decision')
            with self.db() as db:db.execute("UPDATE artifacts SET status='discarded' WHERE id=? AND status='pending'",(ident,))
            return {'message':'不保存进你的正式目录。成果保留在助手工作区和原 Talk 附件中。'}
        if r['status']=='saved':return {'message':'此前已保存到 '+r['saved_path']}
        if r['status']!='pending':raise ValueError('Not pending')
        destination=self.artifact_destination(r)
        self.nc.mkdirs(destination.rsplit('/',1)[0])
        try:self.nc.upload_file(destination,(self.state/'artifacts'/ident).read_bytes())
        except urllib.error.HTTPError as exc:
            if exc.code!=412:raise
            destination=self.reserve_output_path(p,ident,r['name'],replace=True)
            return {'message':'目标位置已有同名文件，未覆盖。请确认新的保存位置。',
                    'artifacts':[{'artifact':ident,'name':r['name'],'save_path':destination}]}
        with self.db() as db:db.execute("UPDATE artifacts SET status='saved',saved_path=? WHERE id=?",(destination,ident))
        return {'message':'已保存到你本人网盘的 '+destination}

    def confirm(self,p):
        with self.db() as db:r=db.execute('SELECT * FROM proposals WHERE id=? AND room=? AND thread=?',(p['artifact'],p['room'],p['thread'])).fetchone()
        if not r or r['status']!='pending':raise PermissionError('No pending change in this task')
        with self.db() as db:db.execute("UPDATE proposals SET status='executing' WHERE id=?",(p['artifact'],))
        result=self.project_change(json.loads(r['data']),p)
        with self.db() as db:db.execute("UPDATE proposals SET status='done' WHERE id=?",(p['artifact'],))
        return result

    def project_change(self,data,p):
        kind=data['kind'];room=p['room']
        if kind in ('create','rename'):
            title=conversation_title(data.get('name',''),main=kind=='rename' and room==self.config['room'])
        if kind=='rename':
            self.nc.ocs('apps/spreed/api/v4/room/'+room,{'roomName':title},'PUT')
            with self.db() as db:db.execute('UPDATE projects SET name=? WHERE room=?',(title,room))
            return {'message':'对话已改名为「'+title+'」。现有上下文保持不变。'}
        if kind=='emoji':
            emoji=data.get('emoji','')
            if not 0<len(emoji)<=16:raise ValueError('Invalid emoji')
            self.nc.ocs('apps/spreed/api/v1/room/'+room+'/avatar/emoji',{'emoji':emoji},'POST')
            return {'message':'当前项目的图标已更新。'}
        if kind=='avatar':
            self.nc.avatar(room,base64.b64decode(data['png'],validate=True));return {'message':'当前项目的图片图标已更新。'}
        if kind=='create':
            created=self.nc.ocs('apps/spreed/api/v4/room',{'roomType':2,'roomName':title},'POST');token=created['token']
            self.nc.ocs('apps/spreed/api/v4/room/'+token+'/participants',{'newParticipant':self.config['assistant_uid'],'source':'users'},'POST')
            self.nc.ocs('apps/spreed/api/v1/bot/'+token+'/'+str(self.config['bot_id']),{},'POST')
            with self.db() as db:db.execute('INSERT INTO projects VALUES (?,?)',(token,title))
            _,_=json_request(self.config['delivery_url']+'/bind',method='POST',headers={'Authorization':'Bearer '+self.config['delivery_token']},
                body={'room':token},timeout=30)
            return {'message':'独立对话「'+title+'」已建立。可以直接说需求，主助理也可把任务交接到这里。','new_project':token}
        raise ValueError('Invalid project operation')

class Handler(BaseHandler):
    def do_GET(self):
        if self.path=='/projects':
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.runner.config['runner_token']):self.reply(401,{});return
            self.reply(200,{'projects':self.server.runner.projects(),'onboarding_complete':self.server.runner.consent() is not None});return
        super().do_GET()

    def do_POST(self):
        if self.path=='/question':
            c=self.server.runner
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+c.config['runner_token']):self.reply(401,{});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if self.headers.get('Transfer-Encoding') or not 0<length<=65536:self.reply(413,{});return
                p=json.loads(self.rfile.read(length));c.authorize(p)
                _,receipt=json_request(c.config['delivery_url']+'/question',method='POST',timeout=60,
                    headers={'Authorization':'Bearer '+c.config['delivery_token']},body={'room':p['room'],'thread':p['thread'],'id':p['job_id'],'message':p['message']})
                self.reply(200,receipt)
            except PermissionError:self.reply(403,{'error':'Question is not authorized'})
            except Exception:self.reply(500,{'error':'Question delivery failed'})
            return
        if self.path=='/consent':
            c=self.server.runner
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+c.config['runner_token']):self.reply(401,{});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if self.headers.get('Transfer-Encoding') or not 0<length<=65536:self.reply(413,{});return
                p=json.loads(self.rfile.read(length));c.authorize(p)
                # Revocation takes effect even while a model task is running. Allow still uses the ordered queue.
                if p.get('decision')!='deny':raise ValueError('Only immediate revocation is supported')
                with c.db() as db:db.execute('INSERT OR REPLACE INTO consent VALUES (?,?,?)',(c.config['uid'],0,time.time()))
                self.reply(200,{'revoked':True})
            except PermissionError:self.reply(403,{'error':'Not authorized'})
            except Exception:self.reply(400,{'error':'Invalid revocation'})
            return
        if self.path=='/execute':return super().do_POST()
        if self.path not in ('/tools','/v1/chat/completions','/v1/responses'):self.reply(404,{});return
        try:
            a=self.server.runner.active_grant(self.headers.get('Authorization',''),self.path=='/tools',self.path.startswith('/v1/'))
            length=int(self.headers.get('Content-Length','0'))
            if self.headers.get('Transfer-Encoding') or not 0<length<=4*1024*1024:self.reply(413,{});return
            raw=self.rfile.read(length)
            if len(raw)!=length:raise ValueError('Truncated request')
            data=json.loads(raw)
            if self.path=='/tools':
                self.reply(200,self.server.runner.tool(a,data['name'],data.get('arguments',{})));return
            # Fixed destination/routes/models; no arbitrary HTTP proxy and no provider key in worker.
            self.server.runner.check_task_model(a,data)
            _,response=request(self.server.runner.config['model_url']+self.path[3:],method='POST',body=raw,timeout=240,limit=32*1024*1024,
                headers={'Content-Type':'application/json','Authorization':'Bearer '+self.server.runner.config['model_token']})
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if data.get('stream') else 'application/json')
            self.send_header('Content-Length',str(len(response)));self.end_headers();self.wfile.write(response)
        except PermissionError as exc:self.reply(403,{'error':str(exc)[:200]})
        except (ValueError,KeyError,TypeError) as exc:self.reply(400,{'error':str(exc)[:200]})
        except Exception as exc:
            print('connector_error='+type(exc).__name__,flush=True);self.reply(500,{'error':'Connector failed'})

def main():
    os.umask(0o077)
    cfg=json.loads(Path('/run/config/connector.json').read_text())
    server=Server(('0.0.0.0',8789),None,Handler);server.runner=Connector(cfg,'/state','/workspace');server.serve_forever()

if __name__=='__main__':main()
