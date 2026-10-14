"""User-owned model choices and immutable, credential-free task model receipts."""
import hashlib
import json
import re
import time

class ModelSettings:
    def init_models(self):
        allowed=self.config.get('allowed_chat_models',[self.config['chat_model']])
        if not isinstance(allowed,list) or not 1<=len(allowed)<=4 or any(not isinstance(m,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',m) for m in allowed):
            raise ValueError('Invalid approved text models')
        self.allowed_models=tuple(dict.fromkeys(allowed))
        if self.config['chat_model'] not in self.allowed_models:raise ValueError('Default model is not approved')
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_preferences (uid TEXT PRIMARY KEY,default_model TEXT NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS model_overrides (uid TEXT,room TEXT,thread INTEGER,model TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(uid,room,thread))')
            db.execute('CREATE TABLE IF NOT EXISTS model_requests (id TEXT PRIMARY KEY,uid TEXT,room TEXT,thread INTEGER,stage TEXT,scope TEXT,options TEXT,status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS task_models (job_id TEXT PRIMARY KEY,uid TEXT,room TEXT,thread INTEGER,model TEXT,root_job TEXT,source TEXT,created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS question_models (question_id TEXT PRIMARY KEY,origin_job TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO model_preferences VALUES (?,?,?)',(self.config['uid'],self.config['chat_model'],time.time()))

    def model_status(self,p):
        with self.db() as db:
            default=db.execute('SELECT default_model FROM model_preferences WHERE uid=?',(self.config['uid'],)).fetchone()[0]
            once=db.execute('SELECT model FROM model_overrides WHERE uid=? AND room=? AND thread=?',(self.config['uid'],p['room'],p['thread'])).fetchone()
        return default,once[0] if once else None

    def model_menu(self,p):
        default,once=self.model_status(p)
        ident=hashlib.sha256((p['job_id']+'\0model-menu').encode()).hexdigest()[:12]
        with self.db() as db:
            db.execute("UPDATE model_requests SET status='superseded' WHERE uid=? AND room=? AND thread=? AND status='pending' AND id<>?",(self.config['uid'],p['room'],p['thread'],ident))
            db.execute('INSERT OR IGNORE INTO model_requests VALUES (?,?,?,?,?,?,?,?)',
                (ident,self.config['uid'],p['room'],p['thread'],'scope',None,json.dumps(['default','once','clear','cancel']),'pending'))
        options=[('1️⃣','设置我的默认模型','default'),('2️⃣','仅本对话下一次任务','once'),('3️⃣','清除本对话单次覆盖','clear'),('❌','保持不变','cancel')]
        text='模型设置\n我的默认：'+default+'\n本对话下一次覆盖：'+(once or '无')+'\n默认适用于你自己的全部项目；单次覆盖仅用于当前对话/线程下一条任务请求，包含自动交接和选择续接。保存、授权等控制操作不消耗覆盖。'
        return self.model_card(ident,'model_scope',text,options)

    def model_card(self,ident,kind,text,options):
        return {'model_card':{'id':ident,'text':text+'\n'+'\n'.join(emoji+' '+label for emoji,label,_ in options),
            'options':{emoji:{'kind':kind,'artifact':ident,'decision':value} for emoji,_,value in options}}}

    def model_decision(self,p):
        expected='scope' if p['kind']=='model_scope' else 'model'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute("SELECT * FROM model_requests WHERE id=? AND uid=? AND room=? AND thread=? AND stage=? AND status='pending'",(p.get('artifact'),self.config['uid'],p['room'],p['thread'],expected)).fetchone()
            if not r or p.get('decision') not in json.loads(r['options']):raise PermissionError('No matching model choice')
            decision=p['decision'];db.execute("UPDATE model_requests SET status='selected' WHERE id=?",(r['id'],))
            if decision=='cancel':return {'message':'模型设置保持不变。'}
            if expected=='scope':
                if decision=='clear':
                    db.execute('DELETE FROM model_overrides WHERE uid=? AND room=? AND thread=?',(self.config['uid'],p['room'],p['thread']))
                    return {'message':'已清除本对话待用的单次模型覆盖；你的默认模型不变。'}
                ident=hashlib.sha256((r['id']+'\0'+decision).encode()).hexdigest()[:12]
                db.execute('INSERT OR IGNORE INTO model_requests VALUES (?,?,?,?,?,?,?,?)',
                    (ident,self.config['uid'],p['room'],p['thread'],'model',decision,json.dumps(list(self.allowed_models)+['cancel']),'pending'))
                emojis=['1️⃣','2️⃣','3️⃣','4️⃣']
                options=[(emojis[i],m,m) for i,m in enumerate(self.allowed_models)]+[('❌','取消','cancel')]
                return self.model_card(ident,'model_select','请选择'+('你自己的默认模型：' if decision=='default' else '本对话下一次任务的模型：'),options)
            if decision not in self.allowed_models:raise PermissionError('Model no longer approved')
            if r['scope']=='default':
                db.execute('UPDATE model_preferences SET default_model=?,updated=? WHERE uid=?',(decision,time.time(),self.config['uid']))
                message='你的默认模型已设为 '+decision+'，对后续任务生效。'
                once=db.execute('SELECT model FROM model_overrides WHERE uid=? AND room=? AND thread=?',(self.config['uid'],p['room'],p['thread'])).fetchone()
                if once:message+='本对话仍有一次待用覆盖：'+once['model']+'。'
            elif r['scope']=='once':
                db.execute('INSERT OR REPLACE INTO model_overrides VALUES (?,?,?,?,?)',(self.config['uid'],p['room'],p['thread'],decision,time.time()))
                message='已为本对话下一次任务设置 '+decision+'。使用一次后恢复你的默认模型；自动交接及选择续接沿用该次任务的模型。'
            else:raise PermissionError('Invalid model scope')
        return {'message':message}

    def assign_task_model(self,p,question_origin=None,is_answer=False):
        uid=self.config['uid']
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM task_models WHERE job_id=?',(p['job_id'],)).fetchone()
            if existing:
                if (existing['uid'],existing['room'],existing['thread'])!=(uid,p['room'],p['thread']):raise PermissionError('Foreign model receipt')
                if existing['model'] not in self.allowed_models:raise PermissionError('Model no longer approved')
                return dict(existing)
            parent=None
            if is_answer:
                if not question_origin:raise PermissionError('Question model origin missing')
                parent=db.execute('SELECT * FROM task_models WHERE job_id=? AND uid=? AND room=? AND thread=?',(question_origin,uid,p['room'],p['thread'])).fetchone()
                if not parent:raise PermissionError('Foreign question model origin')
            elif p.get('inherit_model_from'):
                parent=db.execute('SELECT * FROM task_models WHERE job_id=? AND uid=?',(p['inherit_model_from'],uid)).fetchone()
                h=db.execute('SELECT * FROM handoffs WHERE id=?',(p.get('handoff_id'),)).fetchone()
                if not parent or not h or (h['source_room'],h['source_thread'],h['target_room'],h['target_thread'])!=(parent['room'],parent['thread'],p['room'],p['thread']):
                    raise PermissionError('Foreign handoff model origin')
            if parent:
                model=parent['model'];root=parent['root_job'];source='inherited'
            else:
                once=db.execute('SELECT model FROM model_overrides WHERE uid=? AND room=? AND thread=?',(uid,p['room'],p['thread'])).fetchone()
                model=once['model'] if once else db.execute('SELECT default_model FROM model_preferences WHERE uid=?',(uid,)).fetchone()[0]
                source='once' if once else 'default';root=p['job_id']
            if model not in self.allowed_models:raise PermissionError('Model is not approved')
            if not parent:db.execute('DELETE FROM model_overrides WHERE uid=? AND room=? AND thread=?',(uid,p['room'],p['thread']))
            db.execute('INSERT INTO task_models VALUES (?,?,?,?,?,?,?,?)',(p['job_id'],uid,p['room'],p['thread'],model,root,source,time.time()))
        return {'model':model,'root_job':root,'source':source}

    def check_task_model(self,active,data):
        model=active['payload']['chat_model']
        if model not in self.allowed_models or data.get('model')!=model:raise PermissionError('Model does not match this task')
