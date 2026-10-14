"""One-user runner. Hermes is a short-lived subprocess, never a resident agent."""
from __future__ import annotations
import hmac
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
from .secure_common import MAX_IMAGE_BYTES,Nextcloud
from .secure_bridge import Server

class Runner:
    def __init__(self,config,state):
        self.config=config
        self.state=Path(state)
        self.state.mkdir(parents=True,exist_ok=True)
        self.lock=threading.Lock()
        self.pid=None
        self.home=self.state/'hermes'
        self.home.mkdir(exist_ok=True)
        self.tasks=self.state/'tasks'
        self.tasks.mkdir(exist_ok=True)
        self.nc=Nextcloud(config['nextcloud_url'],config['uid'],config['nextcloud_token'])
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY,room TEXT NOT NULL,status TEXT NOT NULL,path TEXT,share_id INTEGER)')
        self.setup_hermes()

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.state/'artifacts.sqlite',timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def setup_hermes(self):
        # JSON is valid YAML; keep secrets out of the model-readable config and child env.
        hermes={'model':{'provider':'custom','base_url':self.config['model_url'],'default':self.config['chat_model']},
                'database':{'journal_mode':'delete'},
                'agent':{'max_turns':4},'memory':{'memory_enabled':False,'user_profile_enabled':False},
                'compression':{'enabled':False},'display':{'streaming':False},
                'mcp_servers':{'task_image':{'command':self.config.get('python','/opt/hermes/.venv/bin/python'),
                    'args':['-m','nextcloud_talk_hermes_bridge.secure_image_tool'],
                    'timeout':270,'connect_timeout':30,'tools':{'include':['generate_image'],'resources':False,'prompts':False}}}}
        (self.home/'config.yaml').write_text(json.dumps(hermes))
        image={'base_url':self.config['model_url'],'api_key':self.config['model_token'],'image_model':self.config['image_model']}
        (self.state/'image-config.json').write_text(json.dumps(image))
        os.chmod(self.state/'image-config.json',0o600)

    def authorize(self,payload):
        if payload.get('uid')!=self.config['uid'] or payload.get('room')!=self.config['room']:
            raise PermissionError('Identity binding mismatch')
        if not re.fullmatch('[a-f0-9]{12}',payload.get('job_id','')):
            raise ValueError('Invalid job ID')
        self.nc.private_room(self.config['room'])

    def execute(self,payload):
        self.authorize(payload)
        if not self.lock.acquire(False):
            raise BlockingIOError('Runner is busy')
        try:
            if payload.get('kind')=='generate':
                return self.generate(payload)
            if payload.get('kind') in ('save','discard'):
                return self.decide(payload)
            raise ValueError('Invalid action')
        finally:
            self.lock.release()

    def agent(self,job,prompt):
        if not isinstance(prompt,str) or not 1<=len(prompt)<=8000:
            raise ValueError('Invalid prompt')
        python=self.config.get('python','/opt/hermes/.venv/bin/python')
        hermes=json.loads((self.home/'config.yaml').read_text())
        hermes['mcp_servers']['task_image']['env']={'IMAGE_TOOL_CONFIG':str(self.state/'image-config.json'),
            'IMAGE_JOB_ID':job,'IMAGE_TASK_ROOT':str(self.tasks),'PYTHONPATH':'/app:/opt/hermes'}
        (self.home/'config.yaml').write_text(json.dumps(hermes))
        env={'PATH':str(Path(python).parent)+':/usr/local/bin:/usr/bin:/bin','HOME':str(self.home),
             'HERMES_HOME':str(self.home),'PYTHONPATH':'/app:/opt/hermes',
             'OPENAI_API_KEY':self.config['model_token'],'OPENAI_BASE_URL':self.config['model_url'],
             'IMAGE_TOOL_CONFIG':str(self.state/'image-config.json'),'IMAGE_JOB_ID':job,
             'IMAGE_TASK_ROOT':str(self.tasks),'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1',
             'TERM':'dumb','NO_COLOR':'1'}
        instructions=('你是 Nextcloud Talk 图片助手。用户要求生成图片时必须调用 generate_image 工具，一次只生成一张。'
                      '工具会负责生成；宿主负责返回附件、询问是否保存。不要调用文件保存、分享或账户操作。'
                      '不要声称已保存到正式目录。普通聊天用简短中文回答。\n用户消息：\n'+prompt)
        logfile=self.state/'last-agent.log'
        with logfile.open('wb') as output:
            proc=subprocess.Popen([python,'-m','hermes_cli.main','chat','-q',instructions,'--quiet',
                    '--toolsets','task_image','--max-turns','4'],env=env,cwd=self.home,
                    stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            self.pid=proc.pid
            deadline=time.monotonic()+360
            try:
                while proc.poll() is None:
                    if time.monotonic()>deadline or logfile.stat().st_size>2*1024*1024:
                        raise TimeoutError('Agent deadline or output limit')
                    time.sleep(0.5)
                if proc.returncode:
                    raise RuntimeError('Hermes process failed; diagnostic retained locally')
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                self.pid=None
        return logfile.read_text(errors='replace')[-4000:].strip()

    def generate(self,payload):
        job=payload['job_id']
        with self.db() as db:
            existing=db.execute('SELECT * FROM artifacts WHERE id=?',(job,)).fetchone()
            if existing:
                if existing['status']=='pending':
                    return {'artifact':job}
                raise ValueError('Task already attempted; no automatic regeneration')
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)',(job,payload['room'],'running',None,None))
        try:
            message=self.agent(job,payload['prompt'])
        except Exception:
            with self.db() as db:
                db.execute("UPDATE artifacts SET status='failed' WHERE id=?",(job,))
            raise
        target=self.tasks/job/'image.png'
        if not target.exists():
            with self.db() as db:
                db.execute("UPDATE artifacts SET status='no_image' WHERE id=?",(job,))
            return {'message':'这次没有生成图片。请描述你想生成的画面；服务保留了任务状态供排查。'}
        if target.is_symlink() or not target.resolve().is_relative_to(self.tasks.resolve()) or target.stat().st_size>MAX_IMAGE_BYTES:
            raise PermissionError('Invalid artifact')
        binary=target.read_bytes()
        if not binary.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid PNG')
        # Recheck participants after generation, before exposing any user-owned file.
        self.nc.private_room(payload['room'])
        path='AI Temporary/hermes-'+job+'.png'
        self.nc.mkdirs('AI Temporary')
        self.nc.upload(path,binary)
        share=self.nc.share_to_room(path,payload['room'])
        with self.db() as db:
            db.execute("UPDATE artifacts SET status='pending',path=?,share_id=? WHERE id=?",(path,int(share['id']),job))
        return {'artifact':job,'temporary_path':path}

    def decide(self,payload):
        artifact=payload.get('artifact','')
        if not re.fullmatch('[a-f0-9]{12}',artifact):
            raise ValueError('Invalid artifact')
        with self.db() as db:
            row=db.execute('SELECT * FROM artifacts WHERE id=? AND room=?',(artifact,payload['room'])).fetchone()
        if row is None:
            raise PermissionError('Artifact not owned by this conversation')
        if payload['kind']=='discard':
            if row['status']!='pending':
                raise ValueError('Artifact is not awaiting confirmation')
            with self.db() as db:
                db.execute("UPDATE artifacts SET status='discarded' WHERE id=?",(artifact,))
            return {'artifact':artifact}
        if row['status']=='saved':
            return {'artifact':artifact,'saved_path':row['path']}
        if row['status']!='pending':
            raise PermissionError('Artifact is not awaiting save')
        destination='AI Outputs/Images/hermes-'+artifact+'.png'
        self.nc.mkdirs('AI Outputs/Images')
        self.nc.move(row['path'],destination)
        with self.db() as db:
            db.execute("UPDATE artifacts SET status='saved',path=? WHERE id=?",(destination,artifact))
        return {'artifact':artifact,'saved_path':destination}

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
            self.reply(200,{'status':'ok','agent_running':self.server.runner.pid is not None})
        else:
            self.reply(404,{})
    def do_POST(self):
        if self.path!='/execute':
            self.reply(404,{});return
        expected='Bearer '+self.server.runner.config['runner_token']
        if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
            self.reply(401,{});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if self.headers.get('Transfer-Encoding') or not 0<length<=65536:
                self.reply(413,{});return
            raw=self.rfile.read(length)
            if len(raw)!=length:
                self.reply(400,{});return
            result=self.server.runner.execute(json.loads(raw))
            self.reply(200,result)
        except PermissionError:
            self.reply(403,{'error':'Not authorized'})
        except (ValueError,KeyError,TypeError):
            self.reply(400,{'error':'Invalid task'})
        except BlockingIOError:
            self.reply(409,{'error':'Busy'})
        except Exception as exc:
            print('runner_error='+type(exc).__name__,flush=True)
            self.reply(500,{'error':'Task failed'})

def main():
    os.umask(0o077)
    config=json.loads(Path(os.environ.get('RUNNER_CONFIG','/run/config/runner.json')).read_text())
    runner=Runner(config,os.environ.get('RUNNER_STATE','/state'))
    server=Server(('0.0.0.0',8789),None,Handler)
    server.runner=runner
    server.serve_forever()

if __name__=='__main__':
    main()
