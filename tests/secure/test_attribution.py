import base64
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
import urllib.error
import uuid
from unittest import mock
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_context import context_key,task_workspace


class AttributionContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.cfg={'uid':'alice','room':'room123','rooms':['room123'],'runner_token':'dispatch','worker_url':'http://hermes-talk-worker-alice:8790',
            'worker_token':'worker','nextcloud_url':'https://cloud.invalid','nextcloud_token':'nc-secret',
            'assistant_uid':'hermes-assistant','model_url':'http://gateway/v1','model_token':'provider-secret',
            'chat_model':'gpt-6.1-sol','image_model':'gpt-image-2.5','delivery_url':'http://delivery','delivery_token':'delivery'}
        self.c=Connector(self.cfg,self.root/'control',self.root/'workspace')
        with self.c.db() as db:db.execute('INSERT INTO projects VALUES (?,?)',('room456','项目 · 预算'))
        self.p={'uid':'alice','room':'room123','thread':0,'job_id':'a'*12,'chat_model':'gpt-6.1-sol','project_name':'主助理 · Hermes','task_name':'主对话'}

    def envelope(self,p=None,data=None,route='/v1/chat/completions'):
        payload=p or self.p
        raw,seq=self.c.model_envelope({'payload':payload},data or {'model':'gpt-6.1-sol'},route)
        return json.loads(raw),self.c.model_headers(payload['job_id'],seq),seq

    def test_stable_session_is_context_not_turn_model_or_title(self):
        data,headers,_=self.envelope()
        again,other,_=self.envelope({**self.p,'job_id':'b'*12,'project_name':'重新命名'}, {'model':'gpt-5.6-terra'},'/v1/responses')
        self.assertEqual(headers['X-Session-Id'],other['X-Session-Id'])
        self.assertEqual(headers['X-Session-Id'],str(uuid.UUID(context_key('alice','room123',0))))
        self.assertEqual(data['metadata']['session_id'],again['metadata']['session_id'])
        self.assertNotIn('X-Parent-Session-Id',headers)

    def test_main_project_and_thread_form_private_session_tree(self):
        _,main,_=self.envelope()
        _,project,_=self.envelope({**self.p,'room':'room456'})
        _,task,_=self.envelope({**self.p,'room':'room456','thread':25})
        self.assertEqual(project['X-Parent-Session-Id'],main['X-Session-Id'])
        self.assertEqual(task['X-Parent-Session-Id'],project['X-Session-Id'])
        self.assertEqual(len({main['X-Session-Id'],project['X-Session-Id'],task['X-Session-Id']}),3)

    def test_worker_cannot_forge_session_parent_cache_or_source_labels(self):
        body={'model':'gpt-6.1-sol','messages':[{'role':'user','content':'private content'}],
              'metadata':{'session_id':'foreign','parent_session_id':'foreign','hermes_context':'foreign','conversation_name':'foreign'},
              'session_id':'foreign','parentSessionId':'foreign','parent_thread_id':'foreign',
              'conversation':'foreign','prompt_cache_key':'foreign','extra_body':{'parent_id':'foreign'}}
        data,headers,_=self.envelope(data=body)
        self.assertNotIn('foreign',json.dumps(data));self.assertNotIn('foreign',headers['User-Agent'].decode())
        self.assertEqual(data['messages'],body['messages'])

    def test_foreign_user_or_unregistered_room_fails_before_audit(self):
        for change in ({'uid':'bob'},{'room':'room789'}):
            with self.assertRaises(PermissionError):self.envelope({**self.p,**change})
        with self.c.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM model_audit').fetchone()[0],0)

    def test_identical_room_thread_for_two_users_still_has_distinct_sessions(self):
        _,alice,_=self.envelope()
        other=Connector({**self.cfg,'uid':'bob'},self.root/'bob-control',self.root/'bob-workspace')
        _,seq=other.model_envelope({'payload':{**self.p,'uid':'bob'}},{'model':'gpt-6.1-sol'},'/v1/chat/completions')
        self.assertNotEqual(alice['X-Session-Id'],other.model_headers('a'*12,seq)['X-Session-Id'])

    def test_unicode_labels_are_readable_bounded_and_cannot_inject_headers(self):
        _,headers,_=self.envelope({**self.p,'project_name':'项目 · 预算\r\nX-Evil: yes;(forged=1)\x00','task_name':'核对账目'*200})
        value=headers['User-Agent'].decode('utf-8')
        self.assertIn('项目 · 预算',value);self.assertIn('user=alice',value)
        self.assertIn('job='+self.p['job_id']+'; call=1',value)
        self.assertNotIn('\r',value);self.assertNotIn('\n',value);self.assertNotIn('\x00',value)
        self.assertEqual(value.count('('),1);self.assertEqual(value.count(')'),1)
        self.assertLess(len(headers['User-Agent']),700)

    def test_legacy_audit_migration_preserves_old_rows_without_inventing_labels(self):
        state=self.root/'legacy';state.mkdir()
        with sqlite3.connect(state/'assistant.sqlite') as db:
            db.execute('CREATE TABLE model_audit (job TEXT,seq INTEGER,context TEXT,route TEXT,model TEXT,started REAL,status INTEGER,PRIMARY KEY(job,seq))')
            db.execute('INSERT INTO model_audit VALUES (?,?,?,?,?,?,?)',('old',1,'old-context','/v1/responses','gpt-6.1-sol',1,200))
        c=Connector(self.cfg,state,self.root/'legacy-work');c.init_coordination()
        with c.db() as db:
            r=dict(db.execute('SELECT * FROM model_audit').fetchone())
            self.assertEqual(r['context'],'old-context');self.assertEqual(r['status'],200);self.assertIsNone(r['session_id'])

    def test_actual_provider_request_uses_fixed_credentials_and_server_headers(self):
        with mock.patch.object(self.c,'check_task_model'),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,b'{}')) as req:
            self.c.model_request({'payload':self.p},{'model':'gpt-6.1-sol'},'/v1/responses',100)
        self.assertEqual(req.call_args.args[0],'http://gateway/v1/responses')
        self.assertEqual(req.call_args.kwargs['headers']['Authorization'],'Bearer provider-secret')
        self.assertIn('conversation=主助理 · Hermes',req.call_args.kwargs['headers']['User-Agent'].decode())
        with self.c.db() as db:
            row=dict(db.execute('SELECT * FROM model_audit').fetchone())
            self.assertEqual(row['status'],200);self.assertNotIn('secret',json.dumps(row))

    def test_provider_http_and_transport_failures_are_audited_without_retry(self):
        for exc,status in [(urllib.error.HTTPError('gateway',429,'Limited',{},None),429),(OSError('offline'),0)]:
            with mock.patch.object(self.c,'check_task_model'),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',side_effect=exc) as req:
                with self.assertRaises(type(exc)):self.c.model_request({'payload':self.p},{'model':'gpt-6.1-sol'},'/v1/chat/completions',100)
                self.assertEqual(req.call_count,1)
            with self.c.db() as db:self.assertEqual(db.execute('SELECT status FROM model_audit ORDER BY seq DESC').fetchone()[0],status)

    def test_image_generation_shares_context_and_audit_uses_image_model_once(self):
        p={**self.p,'room':'room456','thread':25}
        work=task_workspace(self.c.workspace,'alice','room456',25)
        active={'payload':p,'work':work}
        png=b'\x89PNG\r\n\x1a\n'+b'\x00\x00\x00\rIHDR'+struct.pack('>II',1,1)
        response=json.dumps({'data':[{'b64_json':base64.b64encode(png).decode()}]}).encode()
        with mock.patch.object(self.c.nc,'room'),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,response)) as req:
            self.c.tool(active,'generate_image',{'prompt':'private image prompt'})
            with self.assertRaises(FileExistsError):self.c.tool(active,'generate_image',{'prompt':'private image prompt'})
            self.assertEqual(req.call_count,1)
        with self.c.db() as db:
            row=dict(db.execute('SELECT * FROM model_audit').fetchone())
            self.assertEqual(row['model'],'gpt-image-2.5');self.assertEqual(row['route'],'/v1/images/generations')
            self.assertEqual(row['thread'],25);self.assertEqual(row['status'],200)
            self.assertNotIn('private image prompt',json.dumps(row))

    def test_unsupported_route_and_image_model_never_contact_gateway(self):
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request') as req:
            for route,model in [('/v1/arbitrary','gpt-6.1-sol'),('/v1/images/generations','forged')]:
                with self.assertRaises(PermissionError):self.c.model_request({'payload':self.p},{'model':model},route,100)
            req.assert_not_called()

    def test_empty_stream_recovers_same_model_with_function_call_and_same_authority(self):
        from nextcloud_talk_hermes_bridge.assistant_response import as_stream
        empty=json.dumps({'choices':[{'message':{'content':None},'finish_reason':'stop'}]}).encode()
        recovered=json.dumps({'id':'chatcmpl-test','model':'gpt-6.1-sol','choices':[{'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'call-1','type':'function','function':{'name':'tool_describe','arguments':'{}'}}]},'finish_reason':'tool_calls'}]}).encode()
        data={'model':'gpt-6.1-sol','stream':True,'messages':[{'role':'user','content':'生成图片'}],'tools':[{'function':{'name':'tool_describe'}}]}
        active={'payload':self.p,'models':1,'expires':time.monotonic()+60};self.c.active=active
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',side_effect=[(200,as_stream(empty)),(200,recovered)]) as req:
            status,raw=self.c.model_request(active,data,'/v1/chat/completions',100000)
        self.assertEqual(status,200);self.assertEqual(req.call_count,2);self.assertIn(b'call-1',raw)
        first,second=[json.loads(c.kwargs['body']) for c in req.call_args_list]
        self.assertFalse(second['stream']);self.assertEqual(second['model'],first['model'])
        self.assertEqual(second['messages'][:-1],first['messages']);self.assertEqual(second['tools'],first['tools'])
        self.assertEqual(first['metadata'],second['metadata'])
        for c in req.call_args_list:self.assertEqual(c.kwargs['headers']['Authorization'],'Bearer provider-secret')
        with self.c.db() as db:
            self.assertEqual(db.execute('SELECT status FROM model_recoveries').fetchone()[0],'recovered')
            rows=db.execute('SELECT shape FROM model_response_audit ORDER BY seq').fetchall()
        self.assertEqual(len(rows),2);self.assertNotIn('生成图片',str(rows));self.assertEqual(active['models'],2)

    def test_empty_recovery_reservation_survives_connector_restart_and_never_loops(self):
        empty=json.dumps({'choices':[{'message':{'content':None},'finish_reason':'stop'}]}).encode()
        data={'model':'gpt-6.1-sol','messages':[{'role':'user','content':'task'}]}
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,empty)) as req:
            self.assertEqual(self.c.model_request({'payload':self.p},data,'/v1/chat/completions',10000),(200,empty))
            restarted=Connector(self.cfg,self.root/'control',self.root/'workspace')
            restarted.model_request({'payload':self.p},data,'/v1/chat/completions',10000)
        self.assertEqual(req.call_count,3)  # original + one recovery; next call has no recovery
        with self.c.db() as db:self.assertEqual(db.execute('SELECT status FROM model_recoveries').fetchone()[0],'empty')

    def test_valid_reasoning_or_tool_completion_and_images_never_retried(self):
        for msg in [{'content':'answer'},{'content':None,'reasoning_content':'thinking'},
                    {'content':None,'tool_calls':[{'id':'1'}]}]:
            raw=json.dumps({'choices':[{'message':msg,'finish_reason':'stop'}]}).encode()
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,raw)) as req:
                self.c.model_request({'payload':self.p},{'model':'gpt-6.1-sol','messages':[]},'/v1/chat/completions',10000)
                self.assertEqual(req.call_count,1)
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,b'{}')) as req:
            self.c.model_request({'payload':self.p},{'model':'gpt-image-2.5'},'/v1/images/generations',10000)
            self.assertEqual(req.call_count,1)

    def test_empty_recovery_cannot_exceed_budget_or_change_task_model(self):
        empty=json.dumps({'choices':[{'message':{'content':None},'finish_reason':'stop'}]}).encode()
        data={'model':'gpt-6.1-sol','messages':[]}
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,empty)) as req:
            self.c.model_request({'payload':self.p,'models':32},data,'/v1/chat/completions',10000)
            self.assertEqual(req.call_count,1)
            with self.assertRaises(PermissionError):self.c.model_request({'payload':self.p},{**data,'model':'gpt-5.6-terra'},'/v1/chat/completions',10000)
            self.assertEqual(req.call_count,1)

    def test_empty_recovery_never_runs_after_grant_expiry_or_task_end(self):
        empty=json.dumps({'choices':[{'message':{'content':None},'finish_reason':'stop'}]}).encode()
        data={'model':'gpt-6.1-sol','messages':[]}
        for ended,job in [(False,'b'*12),(True,'c'*12)]:
            active={'payload':{**self.p,'job_id':job},'expires':time.monotonic()+(-1 if not ended else 60)}
            self.c.active=None if ended else active
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.request',return_value=(200,empty)) as req:
                self.c.model_request(active,data,'/v1/chat/completions',10000)
                self.assertEqual(req.call_count,1)


if __name__=='__main__':unittest.main()
