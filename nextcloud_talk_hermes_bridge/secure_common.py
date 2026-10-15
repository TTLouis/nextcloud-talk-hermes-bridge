"""Small shared HTTP/authorization primitives for the secure Talk deployment."""
from __future__ import annotations
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

MAX_IMAGE_BYTES = 20 * 1024 * 1024

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def request(url, *, method='GET', body=None, headers=None, timeout=30, limit=1024*1024):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode()
        headers = {'Content-Type':'application/json', **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        data = response.read(limit+1)
        if len(data) > limit:
            raise ValueError('Response exceeds configured size limit')
        return response.status, data

def json_request(url, **kwargs):
    status, data = request(url, **kwargs)
    return status, json.loads(data) if data else {}

class Nextcloud:
    def __init__(self, base, uid, token):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'https' or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Nextcloud requires a fixed HTTPS origin')
        self.base, self.uid = base.rstrip('/'), uid
        self.headers = {'Authorization':'Basic '+base64.b64encode((uid+':'+token).encode()).decode(),
                        'OCS-APIRequest':'true','Accept':'application/json'}

    def ocs(self, path, data=None, method='GET'):
        headers = dict(self.headers)
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urllib.parse.urlencode(data).encode()
        _, result = json_request(self.base+'/ocs/v2.php/'+path.lstrip('/')+('&' if '?' in path else '?')+'format=json',
                                 method=method, body=body, headers=headers)
        ocs = result.get('ocs', {})
        statuscode = int(ocs.get('meta', {}).get('statuscode',0))
        if statuscode != 100 and not 200 <= statuscode < 300:
            raise RuntimeError('Nextcloud rejected the operation')
        return ocs.get('data')

    def dav_url(self, path):
        if '..' in path.split('/') or '\\' in path:
            raise ValueError('Unsafe file path')
        return self.base+'/remote.php/dav/files/'+urllib.parse.quote(self.uid, safe='')+'/'+urllib.parse.quote(path.lstrip('/'), safe='/')

    def mkdirs(self, path):
        current = ''
        for part in path.strip('/').split('/'):
            current += '/'+part
            try:
                request(self.dav_url(current), method='MKCOL', headers=self.headers)
            except urllib.error.HTTPError as exc:
                if exc.code != 405:
                    raise

    def upload(self, path, data):
        request(self.dav_url(path), method='PUT', body=data,
                headers={**self.headers,'Content-Type':'image/png','If-None-Match':'*'}, limit=8192)

    def share_to_room(self, path, room):
        return self.ocs('apps/files_sharing/api/v1/shares',
                        {'shareType':10,'shareWith':room,'path':'/'+path}, 'POST')

    def move(self, source, destination):
        request(self.dav_url(source), method='MOVE', headers={**self.headers,
                'Destination':self.dav_url(destination),'Overwrite':'F'}, limit=8192)

    def private_room(self, room):
        info = self.ocs('apps/spreed/api/v4/room/'+urllib.parse.quote(room, safe=''))
        if int(info.get('type', 0)) not in (1, 2) or int(info.get('listable', 0)) != 0:
            raise PermissionError('Assistant requires an unlisted, non-public room')
        participants = self.ocs('apps/spreed/api/v4/room/'+urllib.parse.quote(room, safe='')+'/participants')
        for person in participants:
            if person.get('actorType') != 'users' or person.get('actorId') != self.uid:
                raise PermissionError('Another participant has access to the assistant room')
        return True
