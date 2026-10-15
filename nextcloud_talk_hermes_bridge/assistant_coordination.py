"""Explicit handoff result receipts, without importing other task histories."""
import hashlib
import json
import time
from .assistant_context import context_key


class Coordination:
    def init_coordination(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_audit (job TEXT,seq INTEGER,context TEXT,route TEXT,model TEXT,started REAL,status INTEGER,PRIMARY KEY(job,seq))')

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
        # Worker-supplied metadata cannot masquerade as another task.
        data = {**data, 'metadata': {'hermes_context': key, 'hermes_task': p['job_id'],
               'hermes_project': hashlib.sha256((self.config['uid']+'\0'+p['room']).encode()).hexdigest()[:24]}}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM model_audit WHERE job=?',(p['job_id'],)).fetchone()[0]
            db.execute('INSERT INTO model_audit VALUES (?,?,?,?,?,?,?)',
                       (p['job_id'], seq, key, route, p['chat_model'], time.time(), None))
        return json.dumps(data).encode(), seq

    def finish_model_audit(self, job, seq, status):
        with self.db() as db:
            db.execute('UPDATE model_audit SET status=? WHERE job=? AND seq=?', (status, job, seq))
