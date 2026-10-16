import ipaddress
import socket
import unittest
from unittest import mock
import egress_proxy as proxy

def addresses(*ips):return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in ips]

class EgressTests(unittest.TestCase):
    def test_public_resolution_accepts_only_global_unicast(self):
        resolver=mock.Mock(return_value=addresses('93.184.216.34'))
        self.assertEqual(proxy.public_addresses('example.com',443,resolver)[0][4][0],'93.184.216.34')
        resolver.assert_called_once()

    def test_private_reserved_mapped_and_mixed_records_fail_closed(self):
        for ip in ['127.0.0.1','10.0.30.100','169.254.169.254','192.168.1.1','100.64.0.1','0.0.0.0','224.0.0.1','::1','::ffff:127.0.0.1','fe80::1','fc00::1','2002:7f00:1::','2001:db8::1']:
            with self.subTest(ip=ip):
                with self.assertRaises(PermissionError):proxy.public_addresses('example.com',443,lambda *a,**k:addresses('93.184.216.34',ip))

    def test_local_domains_and_non_web_ports_are_denied_before_dns(self):
        for host,port in [('localhost',443),('abc.local',443),('metadata.internal',443),('nextcloud.direct.ttlouis.space.',443),('example.com',22),('example.com',9120)]:
            resolver=mock.Mock()
            with self.assertRaises(PermissionError):proxy.public_addresses(host,port,resolver)
            resolver.assert_not_called()

    def test_pinned_connection_never_resolves_hostname_again(self):
        resolved=addresses('93.184.216.34');sock=mock.Mock()
        with mock.patch.object(proxy,'public_addresses',return_value=resolved) as resolve,mock.patch.object(proxy.socket,'socket',return_value=sock):
            self.assertIs(proxy.connect_public('example.com',443),sock)
            resolve.assert_called_once_with('example.com',443)
            sock.connect.assert_called_once_with(('93.184.216.34',443))

if __name__=='__main__':unittest.main()
