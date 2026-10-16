"""Public-only HTTP CONNECT/GET proxy; resolve once, validate, connect exact IP."""
import ipaddress
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LIMIT=32*1024*1024
SLOTS=threading.BoundedSemaphore(24)

def public_addresses(host,port,resolver=socket.getaddrinfo):
    if not host or len(host)>253 or host.lower().rstrip('.').endswith(('.localhost','.local','.internal','.ttlouis.space')) or host.lower().rstrip('.') in ('localhost','ttlouis.space'):
        raise PermissionError('Private destination')
    if port not in (80,443):raise PermissionError('Unsupported port')
    addresses=resolver(host,port,type=socket.SOCK_STREAM)
    if not addresses:raise PermissionError('No address')
    for family,kind,proto,_,address in addresses:
        ip=ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or (ip.version==6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip not in ipaddress.ip_network('2000::/3'))):
            raise PermissionError('Non-public address')
    return addresses

def connect_public(host,port):
    addresses=public_addresses(host,port)
    for family,kind,proto,_,address in addresses:
        sock=socket.socket(family,kind,proto);sock.settimeout(12)
        try:sock.connect(address);return sock
        except OSError:sock.close()
    raise OSError('Public connection failed')

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def tunnel(self,upstream):
        total=0;deadline=time.monotonic()+65
        sockets=[self.connection,upstream]
        while time.monotonic()<deadline:
            ready,_,_=select.select(sockets,[],[],12)
            if not ready:return
            for source in ready:
                data=source.recv(65536)
                if not data:return
                total+=len(data)
                if total>LIMIT:return
                (upstream if source is self.connection else self.connection).sendall(data)
    def forward(self,connect=False):
        self.close_connection=True
        if not SLOTS.acquire(False):self.send_error(429);return
        upstream=None
        try:
            url=urlsplit(('https://' if connect else '')+self.path)
            if url.scheme not in ('http','https') or url.username or url.password or url.fragment:raise PermissionError()
            port=url.port or (443 if url.scheme=='https' else 80)
            if connect and port!=443:raise PermissionError()
            if not connect and (url.scheme!='http' or port!=80):raise PermissionError()
            upstream=connect_public(url.hostname,port)
            if connect:self.send_response(200);self.end_headers()
            else:
                path=(url.path or '/')+('?' + url.query if url.query else '')
                if any(c in path for c in '\r\n'):raise PermissionError()
                headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','proxy-connection','proxy-authorization','authorization','content-length','transfer-encoding','upgrade')}
                headers.update({'Host':url.hostname,'Connection':'close'})
                raw=self.command+' '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n'
                upstream.sendall(raw.encode('latin1'))
            self.tunnel(upstream)
        except (PermissionError,ValueError):self.send_error(403)
        except Exception:
            try:self.send_error(502)
            except OSError:pass
        finally:
            if upstream:upstream.close()
            SLOTS.release()
    def do_CONNECT(self):self.forward(True)
    def do_GET(self):self.forward()
    def do_HEAD(self):self.forward()
    def do_POST(self):self.send_error(405)
    do_PUT=do_POST
    do_DELETE=do_POST
    do_PATCH=do_POST

if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0',9121),Handler).serve_forever()
