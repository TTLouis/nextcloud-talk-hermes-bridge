"""Explicit handoff result receipts, without importing other task histories."""
import hashlib
import json
import time
import re
import unicodedata
import urllib.parse
import uuid
from .assistant_context import context_key


def audit_label(value, fallback, limit=120):
    """Bounded header-comment text, without controls or delimiter injection."""
    if not isinstance(value,str):return fallback
    value=''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value=re.sub(r'[();=\\]|\s+', ' ', value).strip()
    while len(value.encode('utf-8'))>limit:value=value[:-1]
    return value or fallback


class Coordination:
    def init_coordination(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_audit (job TEXT,seq INTEGER,context TEXT,route TEXT,model TEXT,started REAL,status INTEGER,PRIMARY KEY(job,seq))')
            existing={r['name'] for r in db.execute('PRAGMA table_info(model_audit)')}
            for name,kind in [('room','TEXT'),('thread','INTEGER'),('session_id','TEXT'),('parent_session_id','TEXT'),
                              ('conversation_name','TEXT'),('task_name','TEXT'),('user_label','TEXT')]:
                if name not in existing:db.execute('ALTER TABLE model_audit ADD COLUMN '+name+' '+kind)

    def coordination_brief(self, p):
        with self.db() as db:
            rows = db.execute('SELECT h.id,h.title,h.target_room,h.target_thread,s.status,s.summary,s.updated '
                'FROM handoffs h LEFT JOIN task_state s ON s.room=h.target_room AND s.thread=h.target_thread '
                'JOIN projects p ON p.room=h.target_room WHERE h.source_room=? AND h.source_thread=? '
                'ORDER BY s.updated DESC,h.rowid DESC LIMIT 6', (p['room'], p['thread'])).fetchall()
        out = []
        for r in rows:
            try:
                self.nc.room(r['target_room'], self.config['uid'], self.config['assistant_uid'])
            except Exception:
                continue  # Deleted/restricted projects must not leak cached summaries.
            out.append({'handoff': r['id'], 'task': r['title'], 'room': r['target_room'], 'thread': r['target_thread'],
                        'status': r['status'] or 'queued', 'summary': (r['summary'] or '')[:900], 'updated': r['updated']})
        return out

    def model_envelope(self, active, data, route):
        p = active['payload']; key = context_key(self.config['uid'], p['room'], p['thread'])
        if p['uid']!=self.config['uid']:raise PermissionError('Foreign model identity')
        with self.db() as db:
            project=db.execute('SELECT name FROM projects WHERE room=?',(p['room'],)).fetchone()
        if project is None:raise PermissionError('Unregistered model project')
        session=str(uuid.UUID(key))
        parent_room=p['room'] if p['thread'] else self.config['room']
        parent=str(uuid.UUID(context_key(self.config['uid'],parent_room,0)))
        if parent==session:parent=''
        slug=re.fullmatch(r'hermes-talk-worker-([a-z][a-z0-9-]{0,31})',urllib.parse.urlsplit(self.config['worker_url']).hostname or '')
        user=audit_label(slug[1] if slug else self.config['uid'],'user',64)
        conversation=audit_label(p.get('project_name') or project['name'],'主助理' if p['room']==self.config['room'] else '项目')
        task=audit_label(p.get('task_name'),'线程 '+str(p['thread']) if p['thread'] else '主对话')
        # Session aliases are gateway routing/observability inputs, owned by the
        # authorized Connector, never by Worker-supplied request bodies.
        identity_keys={'extra_body','user','prompt_cache_key','conversation','fork_source'}
        data={k:v for k,v in data.items() if k not in identity_keys and
              not re.match(r'(?i)^(?:session|parent|previous|conversation|thread|forked|task|action|agent|child)[_-]?(?:id|session|thread|task|action|agent|subagent|from)',k)}
        data = {**data, 'metadata': {'hermes_context': key, 'hermes_task': p['job_id'],
               'hermes_project': hashlib.sha256((self.config['uid']+'\0'+p['room']).encode()).hexdigest()[:24],
               'session_id':session, 'parent_session_id':parent}}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM model_audit WHERE job=?',(p['job_id'],)).fetchone()[0]
            db.execute('INSERT INTO model_audit (job,seq,context,route,model,started,status,room,thread,session_id,parent_session_id,conversation_name,task_name,user_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (p['job_id'], seq, key, route, data['model'], time.time(), None,p['room'],p['thread'],session,parent,conversation,task,user))
        return json.dumps(data).encode(), seq

    def model_headers(self, job, seq):
        with self.db() as db:
            r=db.execute('SELECT * FROM model_audit WHERE job=? AND seq=?',(job,seq)).fetchone()
        if r is None:raise PermissionError('Missing request attribution')
        # UTF-8 bytes in a User-Agent comment are retained by this gateway's
        # usage schema. UUID session headers stay ASCII and survive canonicalization.
        source='Hermes-Talk/1 (user='+r['user_label']+'; conversation='+r['conversation_name']+'; room='+r['room']+'; task='+r['task_name']+'; thread='+str(r['thread'])+'; job='+job+'; call='+str(seq)+')'
        headers={'X-Session-Id':r['session_id'],'User-Agent':source.encode('utf-8')}
        if r['parent_session_id']:headers['X-Parent-Session-Id']=r['parent_session_id']
        return headers

    def finish_model_audit(self, job, seq, status):
        with self.db() as db:
            db.execute('UPDATE model_audit SET status=? WHERE job=? AND seq=?', (status, job, seq))
