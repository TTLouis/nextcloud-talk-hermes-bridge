"""Owner-only reaction settings; never exposed as a model tool."""
import hashlib
import json
import time

WEB_MODES={'ask':'首次联网询问一次','auto':'自动批准公开联网','deny':'禁止联网','strict':'逐项确认'}

class AssistantSettings:
    def init_settings(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS web_preferences (uid TEXT PRIMARY KEY,new_project_mode TEXT NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS web_permissions (uid TEXT,room TEXT,mode TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL,PRIMARY KEY(uid,room))')
            db.execute('CREATE TABLE IF NOT EXISTS settings_requests (id TEXT PRIMARY KEY,uid TEXT,room TEXT,thread INTEGER,scope TEXT,options TEXT,status TEXT)')
            db.execute('INSERT OR IGNORE INTO web_preferences VALUES (?,?,?)',(self.config['uid'],'ask',time.time()))
            # Preserve existing projects as ask-first. Future defaults are copied
            # when a project is created; changing defaults is not retroactive.
            for r in db.execute('SELECT room FROM projects').fetchall():
                db.execute('INSERT OR IGNORE INTO web_permissions VALUES (?,?,?,?,?)',(self.config['uid'],r['room'],'ask',1,time.time()))

    def seed_web_project(self,room):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM projects WHERE room=?',(room,)).fetchone():raise PermissionError('Unknown web project')
            mode=db.execute('SELECT new_project_mode FROM web_preferences WHERE uid=?',(self.config['uid'],)).fetchone()[0]
            db.execute('INSERT OR IGNORE INTO web_permissions VALUES (?,?,?,?,?)',(self.config['uid'],room,mode,1,time.time()))

    def web_policy(self,room):
        self.seed_web_project(room)
        with self.db() as db:
            row=db.execute('SELECT mode,revision FROM web_permissions WHERE uid=? AND room=?',(self.config['uid'],room)).fetchone()
        if row['mode'] not in WEB_MODES:raise PermissionError('Invalid web policy')
        return row['mode'],row['revision']

    def set_web_permission(self,db,room,mode,exclude=None):
        if mode not in WEB_MODES:raise ValueError('Invalid web mode')
        changed=db.execute('UPDATE web_permissions SET mode=?,revision=revision+1,updated=? WHERE uid=? AND room=?',(mode,time.time(),self.config['uid'],room)).rowcount
        if not changed:raise PermissionError('Unknown web project')
        # A newer explicit setting invalidates all older pending grants in this
        # project, including other threads. Ordinary assistant choices survive.
        db.execute("UPDATE questions SET status='superseded' WHERE id IN (SELECT id FROM web_requests WHERE room=? AND status='pending' AND id<>?)",(room,exclude or ''))
        db.execute("UPDATE web_requests SET status='cancelled' WHERE room=? AND status IN ('pending','allowed') AND id<>?",(room,exclude or ''))

    def settings_card(self,p,scope,text,options):
        ident=hashlib.sha256((p['job_id']+'\0settings\0'+scope).encode()).hexdigest()[:12]
        with self.db() as db:
            db.execute("UPDATE settings_requests SET status='superseded' WHERE uid=? AND room=? AND thread=? AND status='pending' AND id<>?",(self.config['uid'],p['room'],p['thread'],ident))
            db.execute('INSERT OR IGNORE INTO settings_requests VALUES (?,?,?,?,?,?,?)',(ident,self.config['uid'],p['room'],p['thread'],scope,json.dumps([o[2] for o in options]),'pending'))
        return {'settings_card':{'id':ident,'text':text+'\n'+'\n'.join(e+' '+label for e,label,_ in options),
            'options':{e:{'kind':'settings_select','artifact':ident,'decision':value} for e,_,value in options}}}

    def settings_menu(self,p):
        if p.get('uid')!=self.config['uid']:raise PermissionError('Foreign settings owner')
        model,once=self.model_status(p);mode,_=self.web_policy(p['room'])
        with self.db() as db:default=db.execute('SELECT new_project_mode FROM web_preferences WHERE uid=?',(self.config['uid'],)).fetchone()[0]
        consent=self.consent();drive='未选择' if consent is None else '允许读取' if consent else '未授权'
        text='设置\n默认模型：'+model+('；本任务下一次：'+once if once else '')+'\n本对话联网：'+WEB_MODES[mode]+'\n新项目默认：'+WEB_MODES[default]+'\n网盘：'+drive
        current='主对话联网／自动批准' if p['room']==self.config['room'] else '本项目联网许可'
        return self.settings_card(p,'menu',text,[('1️⃣','模型（默认／单次）','model'),('2️⃣',current,'current'),('3️⃣','新项目默认联网许可','default'),('4️⃣','网盘读取授权','drive'),('❌','关闭','cancel')])

    def settings_decision(self,p):
        if p.get('uid')!=self.config['uid']:raise PermissionError('Foreign settings owner')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute("SELECT * FROM settings_requests WHERE id=? AND uid=? AND room=? AND thread=? AND status='pending'",(p.get('artifact'),self.config['uid'],p['room'],p['thread'])).fetchone()
            if not r or p.get('decision') not in json.loads(r['options']):raise PermissionError('No matching settings choice')
            decision=p['decision'];scope=r['scope']
            db.execute("UPDATE settings_requests SET status='selected' WHERE id=?",(r['id'],))
            if decision=='cancel':return {'message':'设置保持不变。'}
            if scope=='web_current':
                self.set_web_permission(db,p['room'],decision)
                return {'message':'本对话联网已设为「'+WEB_MODES[decision]+'」。适用于本对话的全部任务；其他项目和新项目默认不变。'}
            if scope=='web_default':
                if decision not in WEB_MODES:raise ValueError('Invalid web default')
                db.execute('UPDATE web_preferences SET new_project_mode=?,updated=? WHERE uid=?',(decision,time.time(),self.config['uid']))
                return {'message':'你今后创建项目的默认联网许可已设为「'+WEB_MODES[decision]+'」。已有项目和家人的设置不变。'}
            if scope=='drive':
                if decision not in ('drive_allow','drive_deny'):raise PermissionError('Invalid drive setting')
                allowed=decision=='drive_allow'
                db.execute('INSERT OR REPLACE INTO consent VALUES (?,?,?)',(self.config['uid'],int(allowed),time.time()))
                return {'onboarding_complete':True,'message':'已允许按任务读取本人可访问的网盘；保存仍需单独确认。' if allowed else '已关闭网盘读取；仍可对话、查看直接发送的图片和生图。'}
            if scope!='menu':raise PermissionError('Unknown settings scope')
        if decision=='model':return self.model_menu(p)
        if decision in ('current','default'):
            text=('本对话全部任务的联网许可：' if decision=='current' else '你今后创建项目的默认联网许可（不改已有项目）：')
            text+='\n自动批准会允许助手拟定的公开搜索词和网址。凭据和内网地址仍被拦截，网盘读取及保存另需授权。'
            options=[('1️⃣','自动批准公开联网','auto'),('2️⃣','首次联网询问一次','ask'),('3️⃣','禁止联网','deny'),('4️⃣','逐项确认','strict'),('❌','取消','cancel')]
            return self.settings_card(p,'web_'+decision,text,options)
        if decision=='drive':
            return self.settings_card(p,'drive','是否允许通过 MCP 按任务读取你本人可访问的 Nextcloud 文件？不会自动扫描全盘，也不授予保存或删除权限。',
                [('✅','允许读取','drive_allow'),('🚫','关闭网盘读取','drive_deny'),('❌','取消','cancel')])
        raise PermissionError('Unknown settings selection')
