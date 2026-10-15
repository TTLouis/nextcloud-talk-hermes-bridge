"""Readable public output paths; opaque ownership keys stay in private state."""
from pathlib import Path
import base64
import hashlib
import json
import re
import unicodedata


def readable_component(value, fallback='成果', max_bytes=160):
    if not isinstance(value, str):
        raise ValueError('Invalid name')
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(' ' if c.isspace() else c for c in value if not unicodedata.category(c).startswith('C'))
    value = re.sub(r'[\\/:*?"<>|]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    while len(value.encode('utf-8')) > max_bytes:
        value = value[:-1]
    value = value.rstrip(' .') or fallback
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '文件-' + value
    return value


def readable_filename(path, requested=None):
    suffix = Path(path).suffix
    if not re.fullmatch(r'\.[a-zA-Z0-9]{1,12}', suffix):
        suffix = ''
    name = requested if requested is not None else Path(path).name
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Invalid filename')
    if re.fullmatch(r'[a-f0-9]{12,64}', Path(name).stem):
        name = ('生成图片' if suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp') else '成果') + suffix
    if suffix and not name.lower().endswith(suffix.lower()):
        name = name + suffix
    name = readable_component(name)
    # Keep the true extension even when a long Unicode title was shortened.
    if suffix and not name.lower().endswith(suffix.lower()):
        name = readable_component(name, max_bytes=140) + suffix
    return name


def valid_filename(name):
    return (isinstance(name, str) and bool(name) and len(name.encode('utf-8')) <= 200
            and name == readable_component(name, max_bytes=200) and name not in ('.', '..'))


def numbered_name(name, number):
    if number == 1:
        return name
    p = Path(name)
    return readable_component(p.stem, max_bytes=140) + ' (' + str(number) + ')' + p.suffix


class ReadableOutputs:
    def init_outputs(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS output_locations (room TEXT,thread INTEGER,path TEXT UNIQUE NOT NULL,PRIMARY KEY(room,thread))')
            db.execute('CREATE TABLE IF NOT EXISTS artifact_paths (artifact TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS reserved_output_paths (path TEXT PRIMARY KEY)')

    def output_folder(self, p):
        room = p['room'];thread = p['thread']
        with self.db() as db:
            existing = db.execute('SELECT path FROM output_locations WHERE room=? AND thread=?', (room, thread)).fetchone()
            if existing:
                return existing['path']
        if thread:
            base = self.output_folder({**p, 'thread': 0})
            title = p.get('task_name')
            if not title:
                info = self.nc.ocs('apps/spreed/api/v1/chat/' + room + '/threads/' + str(thread))
                title = info.get('thread', {}).get('title') or info.get('thread', {}).get('name') or '任务'
        else:
            base = 'AI Outputs'
            if room == self.config['room']:
                title = '个人助理'
            else:
                with self.db() as db:
                    project = db.execute('SELECT name FROM projects WHERE room=?', (room,)).fetchone()
                if not project:
                    raise PermissionError('Unknown output project')
                title = re.sub(r'^(?:主助理|项目|主题)\s*[·｜|:：]\s*', '', project['name'])
        title = readable_component(title, fallback='任务' if thread else '项目', max_bytes=120)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT path FROM output_locations WHERE room=? AND thread=?', (room, thread)).fetchone()
            if existing:
                return existing['path']
            number = 1
            while True:
                candidate = base + '/' + (title if number == 1 else title + ' (' + str(number) + ')')
                if not db.execute('SELECT 1 FROM output_locations WHERE path=?', (candidate,)).fetchone():
                    break
                number += 1
            db.execute('INSERT INTO output_locations VALUES (?,?,?)', (room, thread, candidate))
            return candidate

    def reserve_output_path(self, p, artifact, name, replace=False):
        folder = self.output_folder(p)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT path FROM artifact_paths WHERE artifact=?', (artifact,)).fetchone()
            if existing and not replace:
                return existing['path']
            number = 1
            while True:
                candidate = folder + '/' + numbered_name(name, number)
                if not db.execute('SELECT 1 FROM reserved_output_paths WHERE path=?', (candidate,)).fetchone():
                    break
                number += 1
            db.execute('INSERT INTO reserved_output_paths VALUES (?)', (candidate,))
            db.execute('INSERT OR REPLACE INTO artifact_paths VALUES (?,?)', (artifact, candidate))
            return candidate

    def artifact_destination(self, r):
        with self.db() as db:
            target = db.execute('SELECT path FROM artifact_paths WHERE artifact=?', (r['id'],)).fetchone()
        # Preserve the destination already displayed on pre-upgrade save cards.
        return target['path'] if target else 'AI Outputs/' + r['room'] + '/' + r['id'] + '-' + r['name']

    def prepare_snapshot(self, p, artifact, name, data):
        with self.db() as db:
            r = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact,)).fetchone()
        if r:
            if r['room'] != p['room'] or r['thread'] != p['thread']:
                raise PermissionError('Wrong output context')
            return
        snapshot = self.state / 'artifacts' / artifact
        with snapshot.open('xb') as handle:
            handle.write(data)
        with self.db() as db:
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)',
                       (artifact, p['room'], p['thread'], name, 'prepared', hashlib.sha256(data).hexdigest(), None, None))
        self.reserve_output_path(p, artifact, name)

    def deliver_snapshot(self, p, artifact):
        with self.db() as db:
            r = db.execute('SELECT * FROM artifacts WHERE id=? AND room=? AND thread=?',
                           (artifact, p['room'], p['thread'])).fetchone()
        if not r:
            raise PermissionError('Wrong output context')
        if r['status'] == 'prepared':
            data = (self.state / 'artifacts' / artifact).read_bytes()
            if hashlib.sha256(data).hexdigest() != r['sha256']:
                raise ValueError('Corrupted output snapshot')
            _, receipt = self.delivery_request(self.config['delivery_url'] + '/deliver', method='POST', timeout=90, limit=65536,
                headers={'Authorization': 'Bearer ' + self.config['delivery_token']},
                body={'room': p['room'], 'thread': p['thread'], 'artifact': artifact, 'name': r['name'], 'data': base64.b64encode(data).decode()})
            with self.db() as db:
                db.execute("UPDATE artifacts SET status='pending',delivery=? WHERE id=?", (json.dumps(receipt), artifact))
        elif r['status'] not in ('pending', 'saved'):
            raise ValueError('Output is not deliverable')
        return {'artifact': artifact, 'name': r['name'], 'save_path': self.artifact_destination(r)}
