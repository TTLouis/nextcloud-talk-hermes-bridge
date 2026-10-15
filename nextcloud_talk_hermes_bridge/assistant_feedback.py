"""Best-effort task feedback using existing signed bot messages and reactions."""
import threading
import time


class TaskFeedback:
    RECEIVED_REACTION = '👀'
    QUEUED_NOTICE_DELAY = 15
    RUNNING_NOTICE_DELAY = 3
    RUNNING_NOTICE_INTERVAL = 180

    def init_feedback(self):
        self.feedback_lock = threading.Lock()
        self.feedback_wake = threading.Event()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS task_feedback ('
                       'job_id TEXT PRIMARY KEY,received INTEGER NOT NULL DEFAULT 0,'
                       'receipt_attempts INTEGER NOT NULL DEFAULT 0,receipt_next REAL NOT NULL DEFAULT 0,'
                       'started REAL,next_notice REAL NOT NULL DEFAULT 0,queued_sent INTEGER NOT NULL DEFAULT 0)')

    def add_feedback(self, db, job_id, kind):
        if kind in ('task', 'answer'):
            db.execute('INSERT OR IGNORE INTO task_feedback (job_id) VALUES (?)', (job_id,))

    def start_feedback(self, db, job):
        if job['kind'] not in ('task', 'answer'):
            return
        now = time.time()
        # Old held requests may predate this feature. Do not re-acknowledge their old messages.
        db.execute('INSERT OR IGNORE INTO task_feedback (job_id,received) VALUES (?,2)', (job['id'],))
        db.execute('UPDATE task_feedback SET started=?,next_notice=? WHERE job_id=?',
                   (now, now + self.RUNNING_NOTICE_DELAY, job['id']))

    def finish_job(self, job_id, status, error=None):
        # Serialize finalization with bounded feedback sends. No running notice can be
        # sent after the job becomes terminal and the normal result is released.
        with self.feedback_lock:
            with self.db() as db:
                db.execute('UPDATE jobs SET status=?,error=? WHERE id=?', (status, error, job_id))
        self.feedback_wake.set()

    def feedback_destination(self, job):
        room = job['reply_room'] or job['room']
        thread = job['reply_thread'] if job['reply_room'] else job['thread']
        if (job['uid'], job['room']) not in self.bindings or (job['uid'], room) not in self.bindings:
            return None
        if type(thread) is not int or thread < 0 or type(job['message_id']) is not int or job['message_id'] <= 0:
            return None
        return room, thread

    def flush_feedback(self, now=None):
        now = time.time() if now is None else now
        with self.feedback_lock:
            with self.db() as db:
                rows = db.execute("SELECT j.*,f.received,f.receipt_attempts,f.receipt_next,f.started,f.next_notice,f.queued_sent "
                                  "FROM task_feedback f JOIN jobs j ON j.id=f.job_id "
                                  "WHERE j.kind IN ('task','answer') AND j.status IN ('queued','running','awaiting_onboarding') "
                                  "ORDER BY j.created,j.rowid").fetchall()
            for job in rows:
                destination = self.feedback_destination(job)
                if destination is None:
                    continue
                room, thread = destination
                phase = None
                if job['received'] == 0 and job['receipt_attempts'] < 3 and job['receipt_next'] <= now:
                    phase = 'received'
                    with self.db() as db:
                        db.execute('UPDATE task_feedback SET receipt_attempts=receipt_attempts+1,receipt_next=? WHERE job_id=?',
                                   (now + 30, job['id']))
                elif job['status'] == 'queued' and not job['queued_sent'] and now - job['created'] >= self.QUEUED_NOTICE_DELAY:
                    phase = 'queued'
                    message = '已收到，正在排队；轮到这条请求时会开始处理。'
                    with self.db() as db:
                        db.execute('UPDATE task_feedback SET queued_sent=1 WHERE job_id=?', (job['id'],))
                elif job['status'] == 'running' and job['started'] is not None and job['next_notice'] <= now:
                    phase = 'running'
                    elapsed = max(0, int(now - job['started']))
                    if job['next_notice'] <= job['started'] + self.RUNNING_NOTICE_DELAY:
                        message = '正在处理当前请求，完成后会在这里回复。'
                    else:
                        message = '仍在处理当前请求，已运行约 ' + str(max(1, elapsed // 60)) + ' 分钟；完成后会在这里回复。'
                    # Status text is attempted at most once per interval. A failed notice
                    # must never rerun a model task or flood the conversation on recovery.
                    with self.db() as db:
                        db.execute('UPDATE task_feedback SET next_notice=? WHERE job_id=?',
                                   (now + self.RUNNING_NOTICE_INTERVAL, job['id']))
                if phase is None:
                    continue
                try:
                    if phase == 'received':
                        self.react(room, job['message_id'], self.RECEIVED_REACTION, timeout=5)
                        with self.db() as db:
                            db.execute('UPDATE task_feedback SET received=1 WHERE job_id=?', (job['id'],))
                    else:
                        self.post(room, message, thread=thread, timeout=5)
                except Exception:
                    # Optional UI feedback carries no authority. It cannot fail a job,
                    # consume a choice, or log credentials, prompts or other user data.
                    pass
                return True
        return False

    def work_feedback(self):
        while not self.stop.is_set():
            self.feedback_wake.clear()
            try:
                if self.flush_feedback():
                    continue
            except Exception:
                pass
            self.feedback_wake.wait(.5)
