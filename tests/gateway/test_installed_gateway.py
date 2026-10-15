"""No network, no real credentials, no model: installed gateway against a fixed mock."""
import json, pathlib, subprocess, threading, time, urllib.request, urllib.error, sys, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Mock(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        if self.path.endswith('/images/generations'):
            data={'created':1,'data':[{'b64_json':'aW1hZ2U='}],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
        else:
            data={'id':'mock-response','object':'chat.completion','created':1,'model':'mock-model','choices':[{'index':0,'message':{'role':'assistant','content':'mock'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

threading.Thread(target=ThreadingHTTPServer(('127.0.0.1',18317),Mock).serve_forever,daemon=True).start()
cfg={'config-version':8,'server':{'host':'127.0.0.1','port':8317},
     'management':{'secret-key':'offline-management','disable-control-panel':True,'disable-auto-update-panel':True},
     'access':{'api-keys':['offline-client']},'oauth':{'auth-dir':'/tmp/mock-auth'},
     'api-keys':{'openai-compatibility':[{'name':'mock','base-url':'http://127.0.0.1:18317/v1','keys':[{'api-key':'mock-provider'}],'models':[{'name':'mock-model','image':True}]}]},
     'observability':{'usage':{'usage-statistics-enabled':True,'redis-usage-queue-retention-seconds':3600},'logs':{'request-log':False,'logging-to-file':False}}}
pathlib.Path('/tmp/config.json').write_text(json.dumps(cfg))
log=open('/tmp/gateway.log','w+')
proc=subprocess.Popen(['/gateway','--config','/tmp/config.json'],stdout=log,stderr=log)
def call(path,body=None,headers=None):
    req=urllib.request.Request('http://127.0.0.1:8317'+path, data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+('offline-management' if '/management/' in path else 'offline-client'),'Content-Type':'application/json',**(headers or {})})
    with urllib.request.urlopen(req,timeout=10) as r:
        value=json.load(r)
        if body is not None and isinstance(value,dict):value['_test_headers']={k:v for k,v in r.headers.items() if k.lower() in ('x-request-id','x-client-request-id','x-cpa-request-id')}
        return value
try:
    for _ in range(100):
        try:call('/v1/models');break
        except Exception:time.sleep(.05)
    cases=[
      ('body',{'session_id':'Talk · 测试项目 · 线程7','parent_session_id':'Talk · 主助理'},{}),
      ('header',{}, {'X-Session-Id':'hermes-test-project-thread7','X-Parent-Session-Id':'hermes-test-main','X-Request-Id':'hermes-test-request-2','User-Agent':'Hermes-Talk/1'}),
      ('both',{'session_id':'Talk · 项目标题 · 线程8','parent_session_id':'Talk · 主助理'}, {'X-Session-Id':'header-wins','X-Parent-Session-Id':'parent-header-wins'}),
      ('uuid',{'session_id':'forged-body'}, {'X-Session-Id':'11111111-1111-4111-8111-111111111111','X-Parent-Session-Id':'22222222-2222-4222-8222-222222222222','User-Agent':'Hermes-Talk/1 (user=test; project=项目 · 测试; task=线程7)'.encode('utf-8'),'X-Client-Request-Id':'33333333-3333-4333-8333-333333333333'}),
    ]
    out={'cases':[]}
    for name,metadata,headers in cases:
        result=call('/v1/chat/completions',{'model':'mock-model','messages':[{'role':'user','content':'mock input '+name}],'metadata':metadata},headers)
        out['cases'].append({'name':name,'success':result.get('id')=='mock-response','headers':result.get('_test_headers')})
    call('/v1/images/generations',{'model':'mock-model','prompt':'mock image','metadata':{'session_id':'Talk · 图片任务 · 线程9','parent_session_id':'Talk · 主助理'}},{'X-Request-Id':'hermes-test-image-4'})
    sys.path.insert(0,'/src')
    from nextcloud_talk_hermes_bridge.assistant_connector import Connector
    from nextcloud_talk_hermes_bridge.assistant_context import context_key
    connector=Connector({'uid':'alice','room':'room123','rooms':['room123'],'worker_url':'http://hermes-talk-worker-alice:8790',
        'nextcloud_url':'https://cloud.invalid','nextcloud_token':'mock-nextcloud','model_url':'http://127.0.0.1:8317/v1','model_token':'offline-client',
        'chat_model':'mock-model','image_model':'mock-model'},'/tmp/control','/tmp/work')
    with connector.db() as db:db.execute('INSERT INTO projects VALUES (?,?)',('room456','项目 · 合同测试'))
    for room,thread,job,route in [('room123',0,'a'*12,'/v1/chat/completions'),('room456',0,'b'*12,'/v1/chat/completions'),('room456',7,'c'*12,'/v1/images/generations')]:
        p={'uid':'alice','room':room,'thread':thread,'job_id':job,'chat_model':'mock-model','task_name':'核对任务' if thread else '协调'}
        # Task-model authority is covered by secure unit tests; only the fixed mock
        # model is admitted here to test the real outbound HTTP implementation.
        connector.check_task_model=lambda *args:None
        body={'model':'mock-model','messages':[{'role':'user','content':'fixed mock input'}]} if '/chat/' in route else {'model':'mock-model','prompt':'fixed mock image'}
        connector.model_request({'payload':p},body,route,10000)
    time.sleep(.3)
    out['queue']=[]
    for _ in range(10):
        batch=call('/v8/management/observability/usage/queue?limit=100')
        if not batch:break
        out['queue'].extend({k:d.get(k) for k in ('request_id','session_id','parent_session_id','user_agent','endpoint')} for d in batch)
    assert len(out['queue'])==8
    assert out['queue'][3]['session_id']=='11111111-1111-4111-8111-111111111111'
    assert '项目 · 测试' in out['queue'][3]['user_agent']
    expected=[('room123',0),('room456',0),('room456',7)]
    for entry,(room,thread) in zip(out['queue'][-3:],expected):
        assert entry['session_id']==str(uuid.UUID(context_key('alice',room,thread)))
        assert 'Hermes-Talk/1 (user=alice;' in entry['user_agent']
    assert not out['queue'][-3:][0]['parent_session_id']
    assert out['queue'][-2]['parent_session_id']==out['queue'][-3]['session_id']
    assert out['queue'][-1]['parent_session_id']==out['queue'][-2]['session_id']
    assert '项目 · 合同测试' in out['queue'][-1]['user_agent']
    with connector.db() as db:assert db.execute('SELECT COUNT(*) FROM model_audit WHERE status=200').fetchone()[0]==3
    out['connector_to_installed_gateway_contract_passed']=True
    print(json.dumps(out,ensure_ascii=False))
except Exception as exc:
    log.flush();log.seek(0)
    print(json.dumps({'error':str(exc),'gateway_log':log.read()[-4500:]}));raise
finally:
    proc.terminate();proc.wait(timeout=10)
