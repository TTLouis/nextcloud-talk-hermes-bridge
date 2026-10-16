"""Task-owned public research under explicit owner project permissions."""
import base64
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit,urlunsplit,unquote,quote
from .secure_common import json_request
from .assistant_context import write_workspace,read_workspace
from .assistant_outputs import readable_filename
from .assistant_settings import AssistantSettings

ALLOW='允许这一次请求'
DENY='不发送'
PROJECT_ALLOW='允许本对话联网，以后不再问'
PROJECT_DENY='禁止本对话联网'
MAIN_AUTO='主对话与新项目默认自动批准'

def public_url(value):
    if not isinstance(value,str) or not 0<len(value)<=1600 or re.search(r'[\x00-\x20]',value):raise ValueError('Invalid public URL')
    u=urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.port not in (None,80,443):raise ValueError('Unsupported public URL')
    return urlunsplit((u.scheme,u.netloc,u.path or '/',u.query,''))

class PublicWeb(AssistantSettings):
    def init_web(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS web_context (room TEXT,thread INTEGER,private INTEGER NOT NULL,PRIMARY KEY(room,thread))')
            # Existing conversation histories may contain private data. Conservatively
            # label them at migration instead of retrospectively trusting the model.
            db.execute('INSERT OR IGNORE INTO web_context SELECT room,thread,1 FROM task_state')
            db.execute('CREATE TABLE IF NOT EXISTS web_sources (root_job TEXT,room TEXT,thread INTEGER,url TEXT,title TEXT,created REAL,PRIMARY KEY(root_job,room,thread,url))')
            db.execute('CREATE TABLE IF NOT EXISTS web_requests (id TEXT PRIMARY KEY,root_job TEXT,room TEXT,thread INTEGER,request TEXT,status TEXT,response TEXT)')
            columns={r['name'] for r in db.execute('PRAGMA table_info(web_requests)')}
            if 'approval_scope' not in columns:db.execute("ALTER TABLE web_requests ADD COLUMN approval_scope TEXT NOT NULL DEFAULT 'once'")
            if 'policy_revision' not in columns:db.execute('ALTER TABLE web_requests ADD COLUMN policy_revision INTEGER NOT NULL DEFAULT 0')
        self.init_settings()

    def private_web_context(self,p):
        with self.db() as db:r=db.execute('SELECT private FROM web_context WHERE room=? AND thread=?',(p['room'],p['thread'])).fetchone()
        return p['thread']==0 or bool(r and r['private']) or bool(p.get('input_file') or p.get('input_image'))

    def mark_web_private(self,p):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO web_context VALUES (?,?,1)',(p['room'],p['thread']))

    def web_answer(self,p,q):
        if p.get('uid')!=self.config['uid']:raise PermissionError('Foreign outbound choice owner')
        with self.db() as db:
            r=db.execute('SELECT * FROM web_requests WHERE id=? AND room=? AND thread=?',(q['id'],p['room'],p['thread'])).fetchone()
            if r:
                decision=p['decision'];options=(ALLOW,DENY) if r['approval_scope']=='once' else (PROJECT_ALLOW,PROJECT_DENY,ALLOW,DENY,MAIN_AUTO)
                if r['status']!='pending' or decision not in options:raise PermissionError('Invalid outbound approval')
                policy=db.execute('SELECT mode,revision FROM web_permissions WHERE uid=? AND room=?',(self.config['uid'],p['room'])).fetchone()
                if not policy or policy['mode']=='deny' or (r['policy_revision'] and policy['revision']!=r['policy_revision']):raise PermissionError('Outdated outbound approval')
                if decision==MAIN_AUTO and p['room']!=self.config['room']:raise PermissionError('Main-only web default choice')
                if decision in (PROJECT_ALLOW,PROJECT_DENY,MAIN_AUTO):
                    self.set_web_permission(db,p['room'],'deny' if decision==PROJECT_DENY else 'auto',r['id'])
                if decision==MAIN_AUTO:
                    db.execute('UPDATE web_preferences SET new_project_mode=?,updated=? WHERE uid=?',('auto',time.time(),self.config['uid']))
                db.execute('UPDATE web_requests SET status=? WHERE id=?',('denied' if decision in (DENY,PROJECT_DENY) else 'allowed',r['id']))

    def web_tool(self,a,name,args):
        if not self.config.get('browser_url') or not self.config.get('browser_token'):raise PermissionError('Public browser is not configured')
        p=a['payload'];root=p.get('task_root',p['job_id'])
        if p.get('uid')!=self.config['uid']:raise PermissionError('Foreign public task owner')
        mode,revision=self.web_policy(p['room'])
        if mode=='deny':raise PermissionError('This project does not allow public networking; only its owner can change /设置')
        operation={'web_search':'search','web_read':'read','web_download':'download'}[name]
        request={'operation':operation}
        if operation=='search':
            query=args.get('query','')
            if not isinstance(query,str) or not query.strip() or len(query)>300:raise ValueError('Invalid search query')
            request['query']=query.strip()
            explicit=re.fullmatch(r'(?:搜索|search)\s*[:：]\s*(.+)',p.get('prompt','').strip(),re.I|re.S) if p.get('native_input') is True else None
            needs_choice=not (explicit and explicit[1].strip()==request['query'])
        else:
            request['url']=public_url(args.get('url'))
            direct=set()
            # Handoff summaries are model-authored, not user authorization.
            if p.get('native_input') is True:
                for value in re.findall(r'https?://[^\s<>"\'，。；！？、（）「」【】]+',p.get('prompt','')):
                    try:direct.add(public_url(value.rstrip('。，；）')))
                    except ValueError:pass
            with self.db() as db:
                source=db.execute('SELECT 1 FROM web_sources WHERE root_job=? AND room=? AND thread=? AND url=? AND created>?',
                    (root,p['room'],p['thread'],request['url'],time.time()-3600)).fetchone()
            # Strict mode follows exact observed public links or URLs supplied
            # in the current user turn. Broader project consent is checked below.
            needs_choice=request['url'] not in direct and source is None
        encoded=json.dumps(request,sort_keys=True,ensure_ascii=False)
        # Browsing must never disclose an authentication capability, even if a
        # page persuades the model to inspect its environment in a fresh task.
        outbound=unquote(unquote(encoded))
        sensitive=[a.get('token','')]+[self.config.get(k,'') for k in ('nextcloud_token','model_token','runner_token','worker_token','delivery_token','browser_token')]
        for secret in sensitive:
            if not isinstance(secret,str) or len(secret)<16:continue
            forms={secret,secret.upper(),base64.b64encode(secret.encode()).decode(),base64.urlsafe_b64encode(secret.encode()).decode().rstrip('=')}
            if any(value in outbound for value in forms):raise PermissionError('Authentication material cannot be sent to public websites')
        ident=hashlib.sha256((root+'\0'+p['room']+'\0'+str(p['thread'])+'\0'+encoded).encode()).hexdigest()[:12]
        with self.db() as db:old=db.execute('SELECT * FROM web_requests WHERE id=?',(ident,)).fetchone()
        if old and (old['root_job'],old['room'],old['thread'],old['request'])!=(root,p['room'],p['thread'],encoded):raise PermissionError('Foreign public request')
        if old and old['status']=='done':return json.loads(old['response'])
        if old and old['status'] in ('denied','started','failed','cancelled'):raise PermissionError('Public request denied or already attempted; not automatically replayed')
        with self.db() as db:
            denied=db.execute("SELECT 1 FROM web_requests WHERE root_job=? AND room=? AND thread=? AND status='denied'",(root,p['room'],p['thread'])).fetchone()
        if denied:raise PermissionError('The owner declined an outbound request; do not reword it to ask again in this task')
        # Project grants cover all its tasks. Exact legacy one-off approvals stay
        # one-off; migration never silently expands them into project grants.
        needs_choice=mode=='ask' or (mode=='strict' and needs_choice)
        if needs_choice and (not old or old['status']=='pending'):
            displayed=re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]',lambda m:'\\u'+format(ord(m[0]),'04x'),request.get('query',request.get('url','')))
            title=next(r['name'] for r in self.projects() if r['room']==p['room'])
            title=re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]',lambda m:'\\u'+format(ord(m[0]),'04x'),title)
            fence='`'*max(3,1+max((len(v) for v in re.findall(r'`+',title+'\n'+displayed)),default=0))
            question='是否向公网发送助手拟定的以下'+('搜索词' if operation=='search' else '网址')+'？\n'+fence+'text\n'+displayed+'\n'+fence+'\n请勿允许发送令牌、私有文件或完整对话。只授权这一次请求，不授予网盘读取或保存权限。'
            options=[ALLOW,DENY]
            scope='once'
            if mode=='ask':
                scope='project'
                subject='主对话' if p['room']==self.config['room'] else '项目'
                question='首次联网：是否允许以下'+subject+'进行公开搜索、阅读和资料下载？许可适用于本对话全部任务。\n'+fence+'text\n对话：'+title+'\n首次请求：'+displayed+'\n'+fence+'\n允许后可自动发送助手拟定的搜索词和公网网址，不再逐项询问；凭据和内网仍被拦截，网盘读取及保存另需授权。可随时通过 /设置 修改。'
                options=[PROJECT_ALLOW,ALLOW,PROJECT_DENY,DENY]+([MAIN_AUTO] if p['room']==self.config['room'] else [])
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO web_requests (id,root_job,room,thread,request,status,approval_scope,policy_revision) VALUES (?,?,?,?,?,?,?,?)',(ident,root,p['room'],p['thread'],encoded,'pending',scope,revision))
                db.execute('INSERT OR IGNORE INTO questions VALUES (?,?,?,?,?,?,?)',(ident,p['room'],p['thread'],question,json.dumps(options),'pending',None))
                db.execute('INSERT OR IGNORE INTO question_models VALUES (?,?)',(ident,p['job_id']))
            choice={'id':ident,'question':question,'options':options}
            with self.grant_lock:
                if a['questions'] and a['questions'][0]!=choice:raise ValueError('Finish the current choice first')
                if choice not in a['questions']:a['questions'].append(choice)
            return {'awaiting_user_choice':True,'question_id':ident,'instruction':'End this turn. No public request has been sent. Host will show the exact outbound request for owner approval.'}
        with self.db() as db:
            count=db.execute("SELECT COUNT(*) FROM web_requests WHERE root_job=? AND status IN ('started','done','failed')",(root,)).fetchone()[0]
            if count>=8:raise ValueError('Public research request budget exceeded')
            db.execute('INSERT OR IGNORE INTO web_requests (id,root_job,room,thread,request,status) VALUES (?,?,?,?,?,?)',(ident,root,p['room'],p['thread'],encoded,'allowed'))
            db.execute("UPDATE web_requests SET status='started' WHERE id=?",(ident,))
        try:
            _,result=json_request(self.config['browser_url']+'/web',method='POST',timeout=85,limit=29*1024*1024,
                headers={'Authorization':'Bearer '+self.config['browser_token']},body=request)
            source_url=public_url(result['url'])
            references=[{'url':source_url,'title':result.get('title','')}]+result.get('links',[])+result.get('results',[])
            with self.db() as db:
                for item in references[:80]:
                    try:url=public_url(item['url'])
                    except (ValueError,KeyError,TypeError):continue
                    db.execute('INSERT OR REPLACE INTO web_sources VALUES (?,?,?,?,?,?)',(root,p['room'],p['thread'],url,str(item.get('title',''))[:180],time.time()))
            if operation=='download':
                binary=base64.b64decode(result['data'],validate=True)
                if not binary or len(binary)>20*1024*1024:raise ValueError('Invalid download size')
                extensions={'application/pdf':'.pdf','text/plain':'.txt','text/csv':'.csv','application/json':'.json','image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}
                extension=extensions[result['mime']]
                if extension=='.pdf' and not binary.startswith(b'%PDF-'):raise ValueError('Invalid PDF payload')
                if extension=='.png' and not binary.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PNG payload')
                if extension=='.jpg' and not binary.startswith(b'\xff\xd8\xff'):raise ValueError('Invalid JPEG payload')
                if extension=='.webp' and not (binary.startswith(b'RIFF') and binary[8:12]==b'WEBP'):raise ValueError('Invalid WebP payload')
                if extension in ('.txt','.csv','.json'):binary.decode('utf-8-sig')
                suggestion=args.get('name') or Path(unquote(urlsplit(source_url).path)).name or '下载资料'+extension
                filename=readable_filename('download'+extension,suggestion)
                if Path(filename).suffix.lower()!=extension:filename=Path(filename).stem+extension
                dest='downloads/'+ident+'/'+filename
                write_workspace(self.workspace,self.work_relative(a,dest),binary)
                out={'source_url':source_url,'relative_path':dest,'local_path':str(a['work']/'scratch'/dest),'name':filename,'bytes':len(binary),'sha256':hashlib.sha256(binary).hexdigest(),'untrusted_file':True,'saved_to_user_drive':False}
            else:
                out={'source_url':source_url,'untrusted_data':{'title':str(result.get('title',''))[:250],'text':str(result.get('text',''))[:16000],'links':result.get('links',[])[:60],'results':result.get('results',[])[:8]},'truncated':bool(result.get('truncated')),'authority':'public-source-data-only'}
            with self.db() as db:db.execute("UPDATE web_requests SET status='done',response=? WHERE id=?",(json.dumps(out,ensure_ascii=False),ident))
            return out
        except Exception:
            with self.db() as db:db.execute("UPDATE web_requests SET status='failed' WHERE id=?",(ident,))
            raise
