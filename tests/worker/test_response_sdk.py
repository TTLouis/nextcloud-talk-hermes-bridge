"""Exercise recovery SSE through the OpenAI SDK shipped in the pinned Worker."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from openai import OpenAI
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_response import as_stream

class RecoverySDKContract(unittest.TestCase):
    def test_sdk_receives_real_function_call_after_empty_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cfg={'uid':'alice','room':'room123','rooms':['room123'],'runner_token':'dispatch','worker_url':'http://worker',
                 'worker_token':'worker','nextcloud_url':'https://cloud.invalid','nextcloud_token':'test-nc',
                 'assistant_uid':'hermes-assistant','model_url':'http://gateway/v1','model_token':'test-provider',
                 'chat_model':'gpt-6.1-sol','image_model':'gpt-image-2.5','delivery_url':'http://delivery','delivery_token':'test-delivery'}
            connector=Connector(cfg,root/'control',root/'workspace')
            active={'payload':{'uid':'alice','room':'room123','thread':0,'job_id':'a'*12,'chat_model':'gpt-6.1-sol'},'models':1,'expires':time.monotonic()+60}
            connector.active=active
            empty={'id':'chatcmpl-empty','model':'gpt-6.1-sol','created':1,'choices':[{'message':{'role':'assistant','content':None},'finish_reason':'stop'}]}
            recovered={'id':'chatcmpl-recovered','model':'gpt-6.1-sol','created':2,'choices':[{'message':{'role':'assistant','content':None,
                'tool_calls':[{'id':'call-image','type':'function','function':{'name':'mcp__assistant__generate_image','arguments':'{"prompt":"lake"}'}}]},'finish_reason':'tool_calls'}]}
            failures=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    try:
                        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        _,raw=connector.model_request(active,data,'/v1/chat/completions',100000)
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                    except Exception as exc:failures.append(type(exc).__name__);self.send_error(500)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',side_effect=[(200,as_stream(json.dumps(empty).encode())),(200,json.dumps(recovered).encode())]) as provider:
                    with OpenAI(base_url='http://127.0.0.1:'+str(server.server_port)+'/v1',api_key='test',max_retries=0) as client:
                        chunks=list(client.chat.completions.create(model='gpt-6.1-sol',messages=[{'role':'user','content':'make a lake image'}],stream=True))
                    self.assertEqual(provider.call_count,2)
                self.assertFalse(failures)
                call=chunks[0].choices[0].delta.tool_calls[0]
                self.assertEqual(call.index,0);self.assertEqual(call.id,'call-image')
                self.assertEqual(call.function.name,'mcp__assistant__generate_image');self.assertEqual(json.loads(call.function.arguments),{'prompt':'lake'})
                self.assertEqual(chunks[0].choices[0].finish_reason,'tool_calls')
            finally:server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':unittest.main()
