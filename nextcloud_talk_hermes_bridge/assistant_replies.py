"""Mobile presentation with verbatim originals and context-bound, on-demand delivery."""
import hashlib
import re
from .assistant_context import read_workspace, write_workspace
from .assistant_outputs import readable_filename

DETAILS_MARKER = '<!-- assistant-details -->'
MOBILE_LIMIT = 700


def mobile_reply(raw):
    if len(raw) <= MOBILE_LIMIT:
        return raw.strip(), False
    lead, marker, _ = raw.partition(DETAILS_MARKER)
    if marker and lead.strip() and len(lead.strip()) <= MOBILE_LIMIT:
        return lead.strip(), True
    # An excerpt is explicitly labelled: never pretend a mechanical cut is a summary.
    excerpt = raw[:MOBILE_LIMIT - 80]
    boundary = max(excerpt.rfind('\n\n'), excerpt.rfind('\n'))
    if boundary >= 180:
        excerpt = excerpt[:boundary]
    return excerpt.rstrip() + '\n\n（以上为开头节选；完整内容已保留，请点 📄 查看。）', True


def worker_reply(workspace, work, job_id, raw):
    message, long = mobile_reply(raw)
    result = {'message': message}
    if long:
        data = raw.encode('utf-8')
        relative = str(work.relative_to(workspace)) + '/scratch/reply-details/' + job_id + '.md'
        write_workspace(workspace, relative, data)
        result['reply_details'] = {'sha256': hashlib.sha256(data).hexdigest()}
    return result


class ReplyDetails:
    def init_replies(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reply_details (artifact TEXT PRIMARY KEY)')

    def retain_reply(self, p, work, result):
        descriptor = result.pop('reply_details', None)
        if descriptor is None:
            return
        # The worker cannot choose a path or an existing artifact identifier.
        relative = str(work.relative_to(self.workspace)) + '/scratch/reply-details/' + p['job_id'] + '.md'
        data = read_workspace(self.workspace, relative, 8 * 1024 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        if not isinstance(descriptor, dict) or descriptor.get('sha256') != digest:
            raise ValueError('Invalid reply snapshot')
        raw = data.decode('utf-8')
        message, long = mobile_reply(raw)
        if not long:
            raise ValueError('Unexpected reply snapshot')
        ident = hashlib.sha256((p['job_id'] + '\0reply-details').encode()).hexdigest()[:12]
        title = p.get('task_name', '')
        if title in ('', '主助理', '项目协调'):
            title = re.sub(r'^[#*\s]+', '', raw.splitlines()[0])[:32] or '本次回复'
        name = readable_filename('reply.md', '完整说明-' + title + '.md')
        self.prepare_snapshot(p, ident, name, data)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO reply_details VALUES (?)', (ident,))
        result['message'] = message
        result['details'] = {'artifact': ident, 'name': name}

    def view_details(self, p):
        with self.db() as db:
            r = db.execute('SELECT a.* FROM artifacts a JOIN reply_details d ON d.artifact=a.id '
                           'WHERE a.id=? AND a.room=? AND a.thread=?',
                           (p.get('artifact'), p['room'], p['thread'])).fetchone()
        if not r:
            raise PermissionError('Details belong to another task')
        if r['status'] in ('saved', 'discarded'):
            return {'message': '完整说明已在原 Talk 附件中。' + ('此前已保存到 ' + r['saved_path'] if r['status'] == 'saved' else '尚未保存到正式目录。')}
        artifact = self.deliver_snapshot(p, r['id'])
        return {'message': '完整说明已作为附件返回，原文未删减。', 'artifacts': [artifact]}
