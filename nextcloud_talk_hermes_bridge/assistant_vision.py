"""Only the owner's exact native Talk image message can seed a visual task."""
import hashlib
import re
import urllib.error
from .assistant_context import write_workspace

IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')
IMAGE_LIMIT = 10 * 1024 * 1024


def image_reference(content):
    params = content.get('parameters', {})
    if params is None or params==[]:
        return None  # Talk encodes the empty rich-parameter map as a JSON list.
    if not isinstance(params, dict):
        raise ValueError('Invalid attachment parameters')
    file = params.get('file')
    if not isinstance(file, dict) or file.get('type') != 'file':
        return None
    mime = file.get('mimetype', '')
    if mime not in IMAGE_TYPES:
        return None
    ident = str(file.get('id', ''))
    if not re.fullmatch('[1-9][0-9]{0,18}', ident):
        raise ValueError('Invalid image file ID')
    return {'file_id': ident}


def image_magic(data):
    return (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')
            or data.startswith((b'GIF87a', b'GIF89a'))
            or (data.startswith(b'RIFF') and data[8:12] == b'WEBP'))


class TalkImages:
    def resolve_native_thread(self,p):
        mid=p.get('message_id')
        if type(mid) is not int or mid<=0:raise ValueError('Native message ID required')
        try:
            rows=self.nc.ocs('apps/spreed/api/v1/chat/'+p['room']+'/'+str(mid)+'/context?limit=0')
        except urllib.error.HTTPError as exc:
            if exc.code in (304,403,404):raise PermissionError('Native input is unavailable') from None
            raise
        matches=[m for m in rows if str(m.get('id'))==str(mid)] if isinstance(rows,list) else []
        if len(matches)!=1:raise PermissionError('Native input does not match this room')
        m=matches[0]
        if m.get('actorType')!='users' or m.get('actorId')!=self.config['uid'] or m.get('token',p['room'])!=p['room']:
            raise PermissionError('Native input belongs to another user or room')
        actual=int(m.get('threadId',0))
        native_thread=actual
        if actual>0:
            try:info=self.nc.ocs('apps/spreed/api/v1/chat/'+p['room']+'/threads/'+str(actual))
            except urllib.error.HTTPError as exc:
                if exc.code not in (304,403,404):raise
                info={}
            # Ordinary quoted replies also carry their parent's ID. Only an
            # actual named Talk thread creates a separate task context.
            thread=info.get('thread',{}) if isinstance(info,dict) else {}
            if not isinstance(thread,dict) or not (thread.get('title') or thread.get('name')):actual=0
        if actual<0 or native_thread<0 or native_thread>=2**63 or p['thread'] not in (0,actual,native_thread):raise PermissionError('Signed/native task mismatch')
        return actual

    def prepare_talk_image(self, p, work):
        ref = p.get('input_file')
        if ref is None:
            return None
        if not isinstance(ref, dict) or not re.fullmatch('[1-9][0-9]{0,18}', str(ref.get('file_id', ''))):
            raise ValueError('Invalid image reference')
        mid = p.get('message_id')
        if type(mid) is not int or mid <= 0:
            raise ValueError('Image message ID required')
        # Re-fetch one exact native message in this room/thread as this user.
        try:
            history = self.nc.ocs('apps/spreed/api/v1/chat/' + p['room'] + '/' + str(mid)
                                  + '/context?limit=0&threadId=' + str(p['thread']))
        except urllib.error.HTTPError as exc:
            # Talk uses 304 for an empty message context, even without cache headers.
            if exc.code in (304,403,404):raise PermissionError('Image message is unavailable in this task') from None
            raise
        messages = [m for m in history if str(m.get('id')) == str(mid)] if isinstance(history, list) else []
        if len(messages) != 1:
            raise PermissionError('Image message no longer accessible')
        message = messages[0]
        native_thread=int(message.get('threadId',0))
        # REST labels a standalone root with its own ID; the signed webhook
        # uses thread=0 for that same message. Named task replies keep their ID.
        thread_matches=native_thread==p['thread'] or (p['thread']==0 and native_thread==mid)
        if (message.get('actorType') != 'users' or message.get('actorId') != self.config['uid']
                or message.get('token',p['room'])!=p['room'] or not thread_matches):
            raise PermissionError('Image belongs to another sender or task')
        params = message.get('messageParameters', {})
        native_ref = image_reference({'parameters': params})
        if native_ref != ref:
            raise PermissionError('Image reference does not match native message')
        file = params['file']
        if file.get('hide-download') == 'yes':
            raise PermissionError('Image downloads are restricted')
        if int(file.get('size', IMAGE_LIMIT + 1)) > IMAGE_LIMIT:
            raise ValueError('Image exceeds 10 MiB')
        data = self.nc.download_message_image(file['path'], ref['file_id'], file.get('etag'), IMAGE_LIMIT)
        if not image_magic(data):
            raise ValueError('Unsupported image bytes')
        relative = 'incoming/' + p['job_id'] + '.bin'
        write_workspace(self.workspace, str(work.relative_to(self.workspace)) + '/scratch/' + relative, data)
        return {'path': relative, 'sha256': hashlib.sha256(data).hexdigest()}
