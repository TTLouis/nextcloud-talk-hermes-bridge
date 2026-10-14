"""User API adapter behind MCP, with explicit read consent owned by the connector."""
import json
import mimetypes
import secrets
import urllib.parse
from xml.etree import ElementTree as ET
from .secure_common import Nextcloud, request, json_request
from .assistant_context import relative_path

class Cloud(Nextcloud):
    def path(self,value,allow_root=False):
        if value in ('','/') and allow_root:return ''
        if value.startswith('/'):value=value[1:]
        relative_path(value)
        return value

    def list(self,path=''):
        path=self.path(path,True)
        body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:getcontentlength/><d:getcontenttype/><d:resourcetype/><d:getetag/></d:prop></d:propfind>'
        _,raw=request(self.dav_url(path),method='PROPFIND',body=body,
            headers={**self.headers,'Depth':'1','Content-Type':'application/xml'},limit=2*1024*1024)
        root=ET.fromstring(raw);results=[]
        prefix='/remote.php/dav/files/'+self.uid+'/'
        for item in root.findall('{DAV:}response'):
            href=urllib.parse.unquote(item.findtext('{DAV:}href',''))
            if not href.startswith(prefix):continue
            relative=href[len(prefix):].rstrip('/')
            if relative==path.rstrip('/'):continue
            prop=item.find('{DAV:}propstat/{DAV:}prop')
            if prop is None:continue
            results.append({'path':relative,'name':prop.findtext('{DAV:}displayname') or relative.rsplit('/',1)[-1],
                'directory':prop.find('{DAV:}resourcetype/{DAV:}collection') is not None,
                'size':prop.findtext('{DAV:}getcontentlength'),'type':prop.findtext('{DAV:}getcontenttype')})
            if len(results)>=200:break
        return results

    def download(self,path,limit=20*1024*1024):
        path=self.path(path)
        return request(self.dav_url(path),headers=self.headers,limit=limit)[1]

    def upload_file(self,path,binary):
        path=self.path(path)
        request(self.dav_url(path),method='PUT',body=binary,limit=8192,
            headers={**self.headers,'Content-Type':mimetypes.guess_type(path)[0] or 'application/octet-stream','If-None-Match':'*'})

    def room(self,room,owner,assistant):
        info=self.ocs('apps/spreed/api/v4/room/'+urllib.parse.quote(room,safe=''))
        if int(info['type'])!=2 or int(info.get('listable',0))!=0:raise PermissionError('Not a private project room')
        people=self.ocs('apps/spreed/api/v4/room/'+urllib.parse.quote(room,safe='')+'/participants')
        present=set()
        for person in people:
            if person.get('actorType')!='users' or person.get('actorId') not in (owner,assistant):
                raise PermissionError('Unexpected participant')
            present.add(person['actorId'])
        if owner not in present:raise PermissionError('Owner no longer participates')
        return info

    def share_file(self,path,room,thread):
        return self.ocs('apps/files_sharing/api/v1/shares',{'shareType':10,'shareWith':room,'path':'/'+self.path(path),
            'talkMetaData':json.dumps({'threadId':thread})},'POST')

    def avatar(self,room,binary,name='avatar.png'):
        if not binary.startswith(b'\x89PNG\r\n\x1a\n') or len(binary)>4*1024*1024:raise ValueError('Expected bounded PNG')
        boundary='hermes-'+secrets.token_hex(16)
        body=('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="avatar.png"\r\nContent-Type: image/png\r\n\r\n').encode()+binary+('\r\n--'+boundary+'--\r\n').encode()
        _,result=json_request(self.base+'/ocs/v2.php/apps/spreed/api/v1/room/'+room+'/avatar?format=json',method='POST',
            headers={**self.headers,'Content-Type':'multipart/form-data; boundary='+boundary},body=body)
        if result['ocs']['meta']['statuscode'] not in (100,200):raise RuntimeError('Avatar rejected')
