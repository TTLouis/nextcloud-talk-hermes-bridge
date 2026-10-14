"""Project rooms + real Talk task threads; deterministic consent/save confirmations."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse
from .secure_bridge import Bridge as BaseBridge,Denied,Server
from .secure_common import request,json_request
from .assistant_onboarding import INTRO

class Bridge(BaseBridge):
    def __init__(self,config,database):
        super().__init__(config,database)
        with self.db() as db:
            columns={r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
            if 'thread' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN thread INTEGER NOT NULL DEFAULT 0')
            if 'decision' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN decision TEXT')
            for name,kind in [('parent_room','TEXT'),('parent_thread','INTEGER'),('parent_handoff','TEXT'),('reply_room','TEXT'),('reply_thread','INTEGER'),('model_parent_job','TEXT')]:
                if name not in columns:db.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
            db.execute('CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY,room TEXT,message TEXT,reply INTEGER,thread INTEGER,title TEXT,status TEXT,next_attempt REAL,attempts INTEGER)')
            notification_columns={r['name'] for r in db.execute('PRAGMA table_info(notifications)')}
            if 'choice' not in notification_columns:db.execute('ALTER TABLE notifications ADD COLUMN choice TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS choices (room TEXT,message_id INTEGER,uid TEXT,thread INTEGER,execution_room TEXT,execution_thread INTEGER,options TEXT,choice_group TEXT,status TEXT,job_id TEXT,PRIMARY KEY(room,message_id))')
        self.users={b['uid']:b for b in config['bindings']}

    def refresh(self,uid):
        if uid not in self.users:raise Denied('Unknown user')
        b=self.users[uid]
        _,data=json_request(b['runner_url']+'/projects',headers={'Authorization':'Bearer '+b['runner_token']},timeout=8)
        for project in data['projects']:self.bindings[(uid,project['room'])]={**b,'room':project['room']}
        if data.get('onboarding_complete') is True:self.release_onboarding(uid)
        return data['projects']

    def release_onboarding(self,uid):
        # These jobs have never launched Hermes. A completed first-use choice
        # safely releases them; interrupted billable jobs are never retried here.
        with self.db() as db:
            resumed=db.execute("UPDATE jobs SET status='queued' WHERE uid=? AND status='awaiting_onboarding'",(uid,)).rowcount
            db.execute("UPDATE choices SET status='resolved' WHERE uid=? AND choice_group='consent' AND status='pending'",(uid,))
            for n in db.execute("SELECT id,choice FROM notifications WHERE status='pending' AND choice IS NOT NULL").fetchall():
                choice=json.loads(n['choice'])
                if choice['uid']==uid and choice['group']=='consent':
                    db.execute("UPDATE notifications SET status='cancelled' WHERE id=?",(n['id'],))
        return resumed

    def event(self,payload):
        actor=(payload.get('actor') or {}).get('id','') if isinstance(payload,dict) else ''
        if actor=='users/'+self.config['assistant_uid']:return None
        if isinstance(actor,str) and actor.startswith('bots/'):return None
        if isinstance(payload,dict) and payload.get('type')=='Undo':return None
        if isinstance(payload,dict) and payload.get('type')=='Like':
            if not isinstance(actor,str) or not actor.startswith('users/'):raise Denied('Invalid choice actor')
            uid=actor[6:];room=(payload.get('target') or {}).get('id','')
            if (uid,room) not in self.bindings:self.refresh(uid)
            if (uid,room) not in self.bindings:raise Denied('Unknown choice owner/project')
            obj=payload.get('object') or {}
            try:message_id=int(obj['id'])
            except (KeyError,ValueError,TypeError):raise Denied('Invalid choice message')
            emoji=payload.get('content','')
            if not isinstance(emoji,str) or len(emoji)>32:return None
            return {'reaction':True,'uid':uid,'room':room,'message_id':message_id,'emoji':emoji.replace('\ufe0f','')}
        if isinstance(actor,str) and actor.startswith('users/') and (actor[6:],(payload.get('target') or {}).get('id','')) not in self.bindings:
            self.refresh(actor[6:])
        result=super().event(payload)
        if result is None:return None
        thread=(payload.get('object') or {}).get('threadId',0)
        if type(thread) is not int or thread<0 or thread>=2**63:raise Denied('Invalid thread')
        return (*result,thread)

    def enqueue(self,event):
        if isinstance(event,dict) and event.get('reaction'):return self.enqueue_reaction(event)
        uid,room,mid,message,thread=event
        if (uid,room) not in self.bindings:raise Denied('Unbound user/project')
        kind='task';artifact=None;decision=None;prompt=message
        jobid=hashlib.sha256((uid+'\0'+room+'\0'+str(mid)).encode()).hexdigest()[:12]
        with self.db() as db:
            existing=db.execute('SELECT id FROM jobs WHERE uid=? AND room=? AND message_id=?',(uid,room,mid)).fetchone()
            if existing:return existing['id'],False
        if message in ('同意读取网盘','暂不授权','撤销网盘授权'):
            kind='consent';decision='allow' if message=='同意读取网盘' else 'deny'
            if decision=='deny':
                b=self.bindings[(uid,room)]
                json_request(b['runner_url']+'/consent',method='POST',timeout=15,headers={'Authorization':'Bearer '+b['runner_token']},
                    body={'uid':uid,'room':room,'thread':thread,'job_id':jobid,'decision':'deny'})
        elif message.startswith('新任务 '):
            kind='new_task';prompt=message[4:].strip()
            if not 0<len(prompt)<=100:raise Denied('Invalid title')
        elif message.startswith('新项目 '):
            kind='create_project';prompt=message[4:].strip()
        elif message in ('项目列表','任务列表','帮助','使用指南','重新介绍'):kind='help'
        elif message in ('模型','模型设置','切换模型','/model'):kind='model_menu'
        else:
            match=re.fullmatch(r'(保存|不保存|确认操作)\s+([a-f0-9]{12})',message)
            if match:kind={'保存':'save','不保存':'discard','确认操作':'confirm'}[match[1]];artifact=match[2]
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=16:raise Denied('Queue full')
            if kind!='consent' and db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','awaiting_onboarding')").fetchone()[0]>=16:raise Denied('Queue full')
            db.execute('INSERT INTO jobs (id,uid,room,message_id,kind,prompt,artifact,status,created,error,thread,decision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (jobid,uid,room,mid,kind,prompt,artifact,'queued',time.time(),None,thread,decision))
        return jobid,True

    def enqueue_reaction(self,event):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            c=db.execute('SELECT * FROM choices WHERE room=? AND message_id=?',(event['room'],event['message_id'])).fetchone()
            if not c:return 'ignored-reaction',False
            if c['uid']!=event['uid']:raise Denied('This choice belongs to another user')
            if c['status']!='pending':return c['job_id'] or 'resolved-choice',False
            options={k.replace('\ufe0f',''):v for k,v in json.loads(c['options']).items()}
            decision=options.get(event['emoji'])
            if not decision:return 'ignored-reaction',False
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=16:raise Denied('Queue full')
            uid=c['uid'];room=c['execution_room'];thread=c['execution_thread']
            if (uid,room) not in self.bindings:raise Denied('Choice execution project unbound')
            jobid=hashlib.sha256((uid+'\0'+room+'\0'+str(c['message_id'])).encode()).hexdigest()[:12]
            parent=db.execute('SELECT parent_room,parent_thread,parent_handoff FROM jobs WHERE uid=? AND room=? AND thread=? AND parent_room IS NOT NULL ORDER BY created DESC LIMIT 1',(uid,room,thread)).fetchone() if decision['kind']=='answer' else None
            db.execute('INSERT OR IGNORE INTO jobs (id,uid,room,message_id,kind,prompt,artifact,status,created,error,thread,decision,reply_room,reply_thread,parent_room,parent_thread,parent_handoff) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (jobid,uid,room,c['message_id'],decision['kind'],'User selected a bound reaction choice',decision.get('artifact'),'queued',time.time(),None,thread,decision.get('decision'),c['room'],c['thread'],parent['parent_room'] if parent else None,parent['parent_thread'] if parent else None,parent['parent_handoff'] if parent else None))
            db.execute("UPDATE choices SET status='resolved',job_id=? WHERE uid=? AND choice_group=? AND status='pending'",(jobid,uid,c['choice_group']))
            db.execute("UPDATE choices SET status='selected' WHERE room=? AND message_id=?",(c['room'],c['message_id']))
        if decision['kind']=='consent' and decision.get('decision')=='deny':
            b=self.bindings[(uid,room)]
            json_request(b['runner_url']+'/consent',method='POST',timeout=15,headers={'Authorization':'Bearer '+b['runner_token']},
                body={'uid':uid,'room':room,'thread':thread,'job_id':jobid,'decision':'deny'})
        return jobid,True

    def post(self,room,message,reply=0,thread=0,title=None):
        nonce=os.urandom(32).hex();signature=hmac.new(self.config['bot_secret'].encode(),(nonce+message).encode(),hashlib.sha256).hexdigest()
        fields={'message':message}
        if title:fields['threadTitle']=title
        elif reply:fields['replyTo']=reply
        elif thread:fields['threadId']=thread
        request(self.config['nextcloud_url']+'/ocs/v2.php/apps/spreed/api/v1/bot/'+room+'/message',method='POST',
            body=urllib.parse.urlencode(fields).encode(),headers={'OCS-APIRequest':'true','Content-Type':'application/x-www-form-urlencoded',
            'X-Nextcloud-Talk-Bot-Random':nonce,'X-Nextcloud-Talk-Bot-Signature':signature},limit=65536)

    def queue_message(self,job,sequence,message,reply=0,thread=None,title=None,room=None,quote=False):
        ident=job['id']+'-'+str(sequence)
        destination=room or job['reply_room'] or job['room']
        thread=thread if thread is not None else job['reply_thread'] if job['reply_room'] else job['thread']
        if not quote:reply=0
        with self.db() as db:db.execute('INSERT OR IGNORE INTO notifications (id,room,message,reply,thread,title,status,next_attempt,attempts) VALUES (?,?,?,?,?,?,?,?,?)',
            (ident,destination,message,reply,thread,title,'pending',0,0))

    def queue_choice(self,job,sequence,message,options,group,room=None,thread=None):
        destination=room or job['reply_room'] or job['room']
        target_thread=thread if thread is not None else job['reply_thread'] if job['reply_room'] else job['thread']
        self.queue_message(job,sequence,message,thread=target_thread,room=destination)
        data={'uid':job['uid'],'execution_room':job['room'],'execution_thread':job['thread'],'options':options,'group':group}
        with self.db() as db:db.execute('UPDATE notifications SET choice=? WHERE id=?',(json.dumps(data),job['id']+'-'+str(sequence)))

    def queue_model_card(self,job,sequence,card):
        group='models:'+job['room']+':'+str(job['thread'])
        with self.db() as db:
            db.execute("UPDATE choices SET status='resolved' WHERE uid=? AND choice_group=? AND status='pending'",(job['uid'],group))
            for row in db.execute("SELECT id,choice FROM notifications WHERE status='pending' AND choice IS NOT NULL").fetchall():
                choice=json.loads(row['choice'])
                if choice['uid']==job['uid'] and choice['group']==group:db.execute("UPDATE notifications SET status='cancelled' WHERE id=?",(row['id'],))
        self.queue_choice(job,sequence,card['text'],card['options'],group)

    def has_pending_consent(self,uid):
        with self.db() as db:
            if db.execute("SELECT 1 FROM choices WHERE uid=? AND choice_group='consent' AND status='pending'",(uid,)).fetchone():return True
            for row in db.execute("SELECT choice FROM notifications WHERE status='pending' AND choice IS NOT NULL"):
                choice=json.loads(row['choice'])
                if choice['uid']==uid and choice['group']=='consent':return True
        return False

    def react(self,room,message_id,emoji):
        nonce=os.urandom(32).hex();signature=hmac.new(self.config['bot_secret'].encode(),(nonce+emoji).encode(),hashlib.sha256).hexdigest()
        request(self.config['nextcloud_url']+'/ocs/v2.php/apps/spreed/api/v1/bot/'+room+'/reaction/'+str(message_id),method='POST',
            body=urllib.parse.urlencode({'reaction':emoji}).encode(),headers={'OCS-APIRequest':'true','Content-Type':'application/x-www-form-urlencoded',
            'X-Nextcloud-Talk-Bot-Random':nonce,'X-Nextcloud-Talk-Bot-Signature':signature},limit=65536)

    def enqueue_handoff(self,job,handoff):
        if handoff['source_room']!=job['room'] or handoff['source_thread']!=job['thread']:raise Denied('Wrong handoff source')
        target=handoff['target_room'];thread=handoff['target_thread']
        if (job['uid'],target) not in self.bindings or type(thread) is not int or thread<=0:raise Denied('Wrong handoff target')
        child,_=self.enqueue((job['uid'],target,thread,handoff['prompt'],thread))
        with self.db() as db:db.execute('UPDATE jobs SET parent_room=?,parent_thread=?,parent_handoff=?,model_parent_job=? WHERE id=?',
            (job['room'],job['thread'],handoff['id'],job['id'],child))
        return child

    def flush_notification(self):
        with self.db() as db:row=db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1",(time.time(),)).fetchone()
        if not row:return False
        try:
            if row['choice']:
                choice=json.loads(row['choice']);b=self.bindings[(choice['uid'],row['room'])]
                _,receipt=json_request(b['runner_url']+'/question',method='POST',timeout=70,
                    headers={'Authorization':'Bearer '+b['runner_token']},body={'uid':choice['uid'],'room':row['room'],'thread':row['thread'],
                    'job_id':hashlib.sha256(row['id'].encode()).hexdigest()[:12],'message':row['message']})
                with self.db() as db:db.execute('INSERT OR IGNORE INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['room'],receipt['message_id'],choice['uid'],row['thread'],choice['execution_room'],choice['execution_thread'],json.dumps(choice['options']),choice['group'],'pending',None))
                for emoji in choice['options']:self.react(row['room'],receipt['message_id'],emoji)
            else:self.post(row['room'],row['message'],row['reply'],row['thread'],row['title'])
            with self.db() as db:db.execute("UPDATE notifications SET status='sent' WHERE id=?",(row['id'],))
        except Exception:
            with self.db() as db:db.execute('UPDATE notifications SET attempts=attempts+1,next_attempt=? WHERE id=?',(time.time()+min(60,10*(row['attempts']+1)),row['id']))
        return True

    def work(self):
        while not self.stop.is_set():
            notified=False
            for _ in range(16):
                if not self.flush_notification():break
                notified=True
            ran=self.run_one()
            if not notified and not ran:self.stop.wait(.5)

    def run_one(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]:return False
            job=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not job:return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
        b=self.bindings[(job['uid'],job['room'])]
        try:
            awaiting_onboarding=False
            if job['kind']=='new_task':
                self.queue_message(job,0,'任务「'+job['prompt']+'」已建立。请在线程内发送需求；此任务有独立上下文和 scratchpad。首次使用会询问是否允许读取网盘。',title=job['prompt'])
                result={}
            elif job['kind']=='help':
                if job['prompt']=='任务列表':
                    _,result=json_request(b['runner_url']+'/execute',method='POST',headers={'Authorization':'Bearer '+b['runner_token']},
                        body={'uid':job['uid'],'room':job['room'],'thread':job['thread'],'job_id':job['id'],'kind':'status'},timeout=30)
                    self.queue_message(job,0,result['message'],job['message_id'])
                    with self.db() as db:db.execute("UPDATE jobs SET status='done' WHERE id=?",(job['id'],))
                    return True
                projects=self.refresh(job['uid'])
                message=INTRO+'\n\n项目：\n'+'\n'.join('• '+p['name'] for p in projects)+'\n\n发送「模型」或「/model」打开模型选择卡。\n也可使用「新项目 名称」「新任务 标题」手动组织。\n网盘：同意读取网盘／暂不授权／撤销网盘授权。\n附件：保存 编号／不保存 编号。\n项目修改：确认操作 编号。'
                self.queue_message(job,0,message,job['message_id']);result={}
            else:
                _,result=json_request(b['runner_url']+'/execute',method='POST',headers={'Authorization':'Bearer '+b['runner_token']},
                    body={'uid':job['uid'],'room':job['room'],'thread':job['thread'],'message_id':job['message_id'],
                    'job_id':job['id'],'kind':job['kind'],'prompt':job['prompt'],'artifact':job['artifact'],'decision':job['decision'],
                    'inherit_model_from':job['model_parent_job'],'handoff_id':job['parent_handoff']},timeout=700,limit=65536)
                if result.get('new_project'):self.refresh(job['uid'])
                if job['kind']=='consent' and result.get('onboarding_complete') is True:
                    if self.release_onboarding(job['uid']):result['message']+='\n我会按顺序继续你刚才的请求。'
                text=result.get('message','')
                sequence=0
                if result.get('model_card'):
                    self.queue_model_card(job,sequence,result['model_card']);sequence+=1
                if result.get('requires_consent'):
                    awaiting_onboarding=True
                    if not self.has_pending_consent(job['uid']):self.queue_choice(job,-1,text,{'✅':{'kind':'consent','decision':'allow'},'❌':{'kind':'consent','decision':'deny'}},'consent')
                    else:self.queue_message(job,-1,'你的请求已保留。请先在首次欢迎消息下面点 ✅ 或 ❌，我会按顺序继续处理。',job['message_id'])
                    sequence+=1
                elif text:
                    for start in range(0,len(text),3000):
                        self.queue_message(job,sequence,text[start:start+3000],job['message_id']);sequence+=1
                for artifact in result.get('artifacts',[]):
                    text='成果「'+artifact['name']+'」已返回。要保存到「'+artifact.get('save_path','你的正式目录')+'」吗？\n✅ 保存　❌ 不保存'
                    options={'✅':{'kind':'save','artifact':artifact['artifact']},'❌':{'kind':'discard','artifact':artifact['artifact']}}
                    self.queue_choice(job,sequence,text,options,'save:'+artifact['artifact']);sequence+=1
                    if job['parent_room']:
                        self.queue_choice(job,sequence,text,options,'save:'+artifact['artifact'],room=job['parent_room'],thread=job['parent_thread'] or 0);sequence+=1
                for proposal in result.get('proposals',[]):
                    self.queue_choice(job,sequence,proposal['description']+'\n✅ 执行　❌ 取消',{'✅':{'kind':'confirm','artifact':proposal['id']},'❌':{'kind':'cancel','artifact':proposal['id']}},'proposal:'+proposal['id']);sequence+=1
                for question in result.get('questions',[]):
                    emojis=['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'];options={emojis[i]:{'kind':'answer','artifact':question['id'],'decision':label} for i,label in enumerate(question['options'])}
                    text=question['question']+'\n'+'\n'.join(emojis[i]+' '+label for i,label in enumerate(question['options']))
                    self.queue_choice(job,sequence,text,options,'answer:'+question['id']);sequence+=1
                    if job['parent_room']:
                        self.queue_choice(job,sequence,text,options,'answer:'+question['id'],room=job['parent_room'],thread=job['parent_thread'] or 0);sequence+=1
                if result.get('handoffs'):
                    self.refresh(job['uid'])
                    for index,handoff in enumerate(result['handoffs']):
                        self.enqueue_handoff(job,handoff)
                        self.queue_message(job,500+index,'已交接任务「'+handoff['title']+'」。相关背景和下一步已交给独立项目，接下来会自动执行并回报这里。\n'+self.config['nextcloud_url']+'/call/'+handoff['target_room'],job['message_id'])
                if job['parent_room']:
                    self.queue_message(job,1000,'项目任务有更新：\n'+(result.get('message') or '已返回成果，请到项目线程查看。')[:1500]+'\n项目：'+self.config['nextcloud_url']+'/call/'+job['room'],
                        thread=job['parent_thread'] or 0,room=job['parent_room'])
            with self.db() as db:db.execute('UPDATE jobs SET status=? WHERE id=?',('awaiting_onboarding' if awaiting_onboarding else 'done',job['id']))
        except Exception as exc:
            with self.db() as db:db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",(type(exc).__name__,job['id']))
            try:self.queue_message(job,999,'任务未完成（'+job['id']+'）。未自动重试；可以继续说明需求或查看服务状态。',job['message_id'])
            except Exception:pass
            if job['parent_room']:
                try:self.queue_message(job,1001,'交接的项目任务未完成（'+job['id']+'）。项目中保留了任务状态，可继续排查。',thread=job['parent_thread'] or 0,room=job['parent_room'])
                except Exception:pass
        return True

def main():
    os.umask(0o077)
    cfg=json.loads(Path(os.environ.get('BRIDGE_CONFIG','/run/config/bridge.json')).read_text())
    bridge=Bridge(cfg,os.environ.get('BRIDGE_DATABASE','/state/jobs.sqlite'))
    for uid in bridge.users:bridge.refresh(uid)
    threading.Thread(target=bridge.work,daemon=True).start()
    Server(('0.0.0.0',8788),bridge).serve_forever()

if __name__=='__main__':main()
