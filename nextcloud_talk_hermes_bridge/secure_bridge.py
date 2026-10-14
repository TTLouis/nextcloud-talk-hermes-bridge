"""Signed Talk ingress, UID/room bindings, durable deduplication and one global worker."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.parse
from .secure_common import json_request, request

MAX_BODY = 64 * 1024

class Denied(Exception):
    pass

class Bridge:
    def __init__(self, config, database):
        self.config = config
        self.bindings = {(b['uid'],b['room']):b for b in config['bindings']}
        if len(self.bindings) != len(config['bindings']):
            raise ValueError('Duplicate identity binding')
        self.database = str(database)
        self.stop = threading.Event()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, uid TEXT NOT NULL, room TEXT NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL, prompt TEXT NOT NULL, artifact TEXT, status TEXT NOT NULL, created REAL NOT NULL, error TEXT, UNIQUE(uid,room,message_id))')
            db.execute("UPDATE jobs SET status='interrupted', error='Service restarted; not automatically retrying a billable operation' WHERE status='running'")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def verify(self, headers, raw):
        nonce = headers.get('X-Nextcloud-Talk-Random','')
        signature = headers.get('X-Nextcloud-Talk-Signature','')
        if not 16 <= len(nonce) <= 256 or not re.fullmatch('[a-fA-F0-9]{64}',signature):
            return False
        expected = hmac.new(self.config['bot_secret'].encode(),nonce.encode()+raw,hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.lower(),expected)

    def event(self, payload):
        if not isinstance(payload,dict) or payload.get('type') != 'Create':
            return None
        actor = payload.get('actor') or {}
        aid = actor.get('id','')
        if not isinstance(aid,str):
            raise Denied('Invalid actor')
        if aid.startswith('bots/'):
            return None
        if not aid.startswith('users/'):
            raise Denied('Only bound Nextcloud users are allowed')
        uid = aid[6:]
        room = (payload.get('target') or {}).get('id','')
        if (uid,room) not in self.bindings:
            raise Denied('Sender and room binding mismatch')
        obj = payload.get('object') or {}
        if obj.get('type') != 'Note':
            return None
        try:
            content = json.loads(obj.get('content','{}'))
            message = content.get('message','').strip()
            mid = int(obj.get('id',0))
        except (TypeError,ValueError,AttributeError):
            raise Denied('Malformed message')
        if not isinstance(message,str) or not message or len(message)>8000 or mid<=0:
            raise Denied('Invalid message')
        # File/system share notifications must not launch a billable agent task.
        if message in ('{file}','file_shared'):
            return None
        return uid,room,mid,message

    def enqueue(self, event):
        uid,room,mid,message = event
        if (uid,room) not in self.bindings:
            raise Denied('Unbound sender')
        kind, artifact = 'generate',None
        match = re.fullmatch(r'(保存|save|不保存|discard)(?:\s+([a-f0-9]{12}))?',message,re.I)
        if match:
            kind = 'save' if match.group(1).lower() in ('保存','save') else 'discard'
            with self.db() as db:
                rows = db.execute("SELECT id,artifact FROM jobs WHERE uid=? AND room=? AND status='awaiting_save'"+(" AND id=?" if match.group(2) else ''),
                                  (uid,room,match.group(2)) if match.group(2) else (uid,room)).fetchall()
            if len(rows)!=1:
                raise Denied('请回复图片消息中的完整保存编号。')
            artifact = rows[0]['id']
        jobid = hashlib.sha256((uid+'\0'+room+'\0'+str(mid)).encode()).hexdigest()[:12]
        with self.db() as db:
            existing = db.execute('SELECT id FROM jobs WHERE uid=? AND room=? AND message_id=?',(uid,room,mid)).fetchone()
            if existing:
                return existing['id'],False
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if active>=16:
                raise Denied('Queue is full')
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (jobid,uid,room,mid,kind,message,artifact,'queued',time.time(),None))
        return jobid,True

    def post(self, room, message, reply=0):
        nonce = os.urandom(32).hex()
        signature = hmac.new(self.config['bot_secret'].encode(),(nonce+message).encode(),hashlib.sha256).hexdigest()
        fields = {'message':message}
        if reply:
            fields['replyTo']=reply
        headers={'X-Nextcloud-Talk-Bot-Random':nonce,'X-Nextcloud-Talk-Bot-Signature':signature,
                 'OCS-APIRequest':'true','Content-Type':'application/x-www-form-urlencoded'}
        request(self.config['nextcloud_url']+'/ocs/v2.php/apps/spreed/api/v1/bot/'+room+'/message',
                method='POST',body=urllib.parse.urlencode(fields).encode(),headers=headers,limit=65536)

    def run_one(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]:
                return False
            job = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if job is None:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
        b = self.bindings[(job['uid'],job['room'])]
        try:
            _, result = json_request(b['runner_url']+'/execute',method='POST',headers={'Authorization':'Bearer '+b['runner_token']},
                body={'uid':job['uid'],'room':job['room'],'job_id':job['id'],'kind':job['kind'],
                      'prompt':job['prompt'],'artifact':job['artifact']},timeout=420,limit=65536)
            if job['kind']=='generate' and result.get('artifact'):
                status='awaiting_save'
                message='图片已返回，暂存在你的 AI Temporary 文件夹。要保存到正式图片目录吗？\n回复「保存 '+job['id']+'」或「不保存 '+job['id']+'」。'
            elif job['kind']=='generate':
                status='done'
                message=result.get('message','请描述你想生成的图片。')[:4000]
            elif job['kind']=='save':
                status='done'
                message='已保存到你的 '+result['saved_path']
            else:
                status='done'
                message='好的，不移入正式目录。图片仍在你的 AI Temporary 文件夹中，后续可以继续使用。'
            with self.db() as db:
                db.execute('UPDATE jobs SET status=?,artifact=? WHERE id=?',(status,result.get('artifact',job['artifact']),job['id']))
                if job['kind'] in ('save','discard'):
                    db.execute("UPDATE jobs SET status=? WHERE id=? AND uid=? AND room=?",('saved' if job['kind']=='save' else 'discarded',job['artifact'],job['uid'],job['room']))
            try:
                self.post(job['room'],message,job['message_id'])
            except Exception as notification_error:
                with self.db() as db:
                    db.execute('UPDATE jobs SET error=? WHERE id=?',('Notification_'+type(notification_error).__name__,job['id']))
        except Exception as exc:
            # Never log prompts, credentials, model output or upstream response bodies.
            with self.db() as db:
                db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",(type(exc).__name__,job['id']))
            try:
                self.post(job['room'],'这个任务未能完成（编号 '+job['id']+'）。没有自动重试，请稍后重试或查看服务状态。',job['message_id'])
            except Exception:
                pass
        return True

    def work(self):
        while not self.stop.is_set():
            if not self.run_one():
                self.stop.wait(0.5)

    def stats(self):
        with self.db() as db:
            return dict(db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall())

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    def __init__(self,address,bridge,handler=None):
        self.bridge=bridge
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(address,handler or Handler)
    def process_request(self,request,client_address):
        if not self.slots.acquire(False):
            request.close();return
        super().process_request(request,client_address)
    def process_request_thread(self,request,client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self.slots.release()

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(10)
    def log_message(self,*args):
        pass
    def reply(self,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/heartbeat':
            self.reply(200,{'status':'ok','max_concurrent_jobs':1})
        else:
            self.reply(404,{})
    def do_PUT(self):
        self.reply(403,{'error':'Lifecycle endpoints are not exposed'})
    def do_POST(self):
        if self.path!='/hook':
            self.reply(404,{});return
        try:
            if self.headers.get('Transfer-Encoding'):
                raise Denied('Chunked requests are not accepted')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY:
                self.reply(413,{});return
            raw=self.rfile.read(length)
            if len(raw)!=length or not self.server.bridge.verify(self.headers,raw):
                self.reply(401,{});return
            event=self.server.bridge.event(json.loads(raw))
            if event:
                jobid,created=self.server.bridge.enqueue(event)
                self.reply(202,{'accepted':True,'job_id':jobid,'new':created})
            else:
                self.reply(202,{'accepted':False})
        except Denied:
            self.reply(403,{'error':'Identity, room or action is not authorized'})
        except (ValueError,TypeError,KeyError):
            self.reply(400,{})

def main():
    os.umask(0o077)
    config=json.loads(Path(os.environ.get('BRIDGE_CONFIG','/run/config/bridge.json')).read_text())
    database=Path(os.environ.get('BRIDGE_DATABASE','/state/jobs.sqlite'))
    database.parent.mkdir(parents=True,exist_ok=True)
    bridge=Bridge(config,database)
    threading.Thread(target=bridge.work,daemon=True).start()
    Server(('0.0.0.0',8788),bridge).serve_forever()

if __name__=='__main__':
    main()
