import hashlib,hmac,io,json,os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from nextcloud_talk_hermes_bridge.secure_bridge import Bridge,Denied,Handler
from nextcloud_talk_hermes_bridge.secure_runner import Runner
from nextcloud_talk_hermes_bridge.secure_common import Nextcloud,NoRedirect
from nextcloud_talk_hermes_bridge import secure_image_tool

class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config={'bot_secret':'synthetic-secret','nextcloud_url':'https://cloud.invalid',
             'bindings':[{'uid':'alice','room':'private-a','runner_url':'http://runner','runner_token':'synthetic-runner'}]}
        self.bridge=Bridge(self.config,self.root/'jobs.sqlite')

    def event(self,uid='alice',room='private-a',mid=1,message='画一只猫'):
        return {'type':'Create','actor':{'id':'users/'+uid,'name':'Same name'},
                'target':{'id':room},'object':{'type':'Note','id':str(mid),'content':json.dumps({'message':message})}}

    def test_sender_room_authorization_uses_uid_not_display_name(self):
        self.assertEqual(self.bridge.event(self.event())[0],'alice')
        for event in [self.event('bob'),self.event(room='private-b')]:
            with self.assertRaises(Denied):self.bridge.event(event)
        event=self.event();event['actor']['id']='guests/alice'
        with self.assertRaises(Denied):self.bridge.event(event)

    def test_valid_signature_and_modified_body(self):
        raw=json.dumps(self.event()).encode();nonce='a'*64
        signature=hmac.new(b'synthetic-secret',nonce.encode()+raw,hashlib.sha256).hexdigest()
        headers={'X-Nextcloud-Talk-Random':nonce,'X-Nextcloud-Talk-Signature':signature}
        self.assertTrue(self.bridge.verify(headers,raw))
        self.assertFalse(self.bridge.verify(headers,raw+b' '))
        self.assertFalse(self.bridge.verify({},raw))

    def test_replay_is_deduplicated_across_restart(self):
        event=self.bridge.event(self.event())
        first=self.bridge.enqueue(event)
        other=Bridge(self.config,self.root/'jobs.sqlite')
        second=other.enqueue(event)
        self.assertTrue(first[1]);self.assertFalse(second[1]);self.assertEqual(first[0],second[0])

    def test_save_action_cannot_target_another_users_artifact(self):
        own,_=self.bridge.enqueue(self.bridge.event(self.event()))
        with self.bridge.db() as db:db.execute("UPDATE jobs SET status='awaiting_save' WHERE id=?",(own,))
        self.bridge.enqueue(self.bridge.event(self.event(mid=2,message='保存 '+own)))
        with self.assertRaises(Denied):
            self.bridge.enqueue(('bob','private-a',3,'保存 '+own))
        with self.assertRaises(Denied):
            self.bridge.enqueue(('alice','private-b',3,'保存 '+own))

    def test_single_global_worker_even_when_run_one_called_concurrently(self):
        self.bridge.enqueue(self.bridge.event(self.event()))
        self.bridge.enqueue(self.bridge.event(self.event(mid=2)))
        entered=threading.Event();release=threading.Event()
        def execute(*a,**kw):entered.set();release.wait(2);return 200,{'artifact':'synthetic'}
        with mock.patch('nextcloud_talk_hermes_bridge.secure_bridge.json_request',side_effect=execute) as call, mock.patch.object(self.bridge,'post'):
            thread=threading.Thread(target=self.bridge.run_one);thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.bridge.run_one())
            release.set();thread.join(2)
            self.assertEqual(call.call_count,1)

    def fake_handler(self,length):
        handler=object.__new__(Handler)
        handler.path='/hook';handler.headers={'Content-Length':str(length)}
        handler.rfile=mock.Mock();handler.reply=mock.Mock()
        handler.server=mock.Mock(bridge=self.bridge)
        return handler

    def test_invalid_or_oversized_body_rejected_without_reading(self):
        for length in (-1,0,65537):
            handler=self.fake_handler(length);handler.do_POST()
            handler.rfile.read.assert_not_called();self.assertEqual(handler.reply.call_args.args[0],413)

    def test_lifecycle_and_delivery_endpoints_disabled(self):
        handler=self.fake_handler(1)
        handler.path='/enabled?enabled=0';handler.do_PUT()
        self.assertEqual(handler.reply.call_args.args[0],403)
        handler.path='/deliver';handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0],404)
        handler.rfile.read.assert_not_called()

    def runner(self,uid='alice',room='private-a'):
        config={'uid':uid,'room':room,'runner_token':'synthetic-runner','nextcloud_url':'https://cloud.invalid',
                'nextcloud_token':'synthetic-nc','model_url':'http://gateway/v1','model_token':'synthetic-model',
                'chat_model':'gpt-5.6-terra','image_model':'gpt-image-2.5','python':'/usr/bin/python3'}
        return Runner(config,self.root/uid)

    def test_runner_independently_checks_identity_room_and_participants(self):
        runner=self.runner()
        for uid,room in [('bob','private-a'),('alice','private-b')]:
            with self.assertRaises(PermissionError):runner.execute({'uid':uid,'room':room,'job_id':'a'*12})
        with mock.patch.object(runner.nc,'private_room',side_effect=PermissionError('new participant')):
            with self.assertRaises(PermissionError):runner.execute({'uid':'alice','room':'private-a','job_id':'a'*12})
        self.assertIsNone(runner.pid)

    def test_new_user_has_separate_workspace_credentials_and_artifact_database(self):
        a=self.runner();b=self.runner('bob','private-b')
        self.assertNotEqual(a.home,b.home)
        with a.db() as db:db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)',('a'*12,'private-a','pending','AI Temporary/test.png',1))
        with mock.patch.object(b.nc,'private_room',return_value=True):
            with self.assertRaises(PermissionError):b.execute({'uid':'bob','room':'private-b','job_id':'b'*12,'kind':'save','artifact':'a'*12})

    def test_generation_returns_attachment_before_any_formal_save(self):
        runner=self.runner();job='a'*12
        def agent(*args):
            target=runner.tasks/job/'image.png';target.parent.mkdir();target.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic');return 'done'
        payload={'uid':'alice','room':'private-a','job_id':job,'kind':'generate','prompt':'cat'}
        with mock.patch.object(runner,'agent',side_effect=agent),mock.patch.object(runner.nc,'private_room',return_value=True), \
             mock.patch.object(runner.nc,'mkdirs') as mkdirs,mock.patch.object(runner.nc,'upload') as upload, \
             mock.patch.object(runner.nc,'share_to_room',return_value={'id':17}) as share,mock.patch.object(runner.nc,'move') as move:
            result=runner.execute(payload)
            self.assertEqual(result['artifact'],job)
            upload.assert_called_once();share.assert_called_once_with('AI Temporary/hermes-'+job+'.png','private-a')
            mkdirs.assert_called_once_with('AI Temporary');move.assert_not_called()
        with mock.patch.object(runner.nc,'private_room',return_value=True),mock.patch.object(runner.nc,'mkdirs'),mock.patch.object(runner.nc,'move') as move:
            result=runner.execute({**payload,'job_id':'b'*12,'kind':'save','artifact':job})
            move.assert_called_once_with('AI Temporary/hermes-'+job+'.png','AI Outputs/Images/hermes-'+job+'.png')
            self.assertTrue(result['saved_path'].startswith('AI Outputs/Images/'))

    def test_image_tool_bills_once_and_ignores_model_supplied_destinations(self):
        cfg=self.root/'image.json';cfg.write_text(json.dumps({'base_url':'http://gateway/v1','api_key':'test','image_model':'gpt-image-2.5'}))
        import base64
        with mock.patch.dict(os.environ,{'IMAGE_TOOL_CONFIG':str(cfg),'IMAGE_JOB_ID':'a'*12,'IMAGE_TASK_ROOT':str(self.root/'tasks')}), \
             mock.patch('nextcloud_talk_hermes_bridge.secure_image_tool.json_request',return_value=(200,{'data':[{'b64_json':base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic').decode()}]})) as call:
            self.assertTrue(secure_image_tool.generate('cat')['success'])
            self.assertTrue(secure_image_tool.generate('cat')['already_generated'])
            self.assertEqual(call.call_count,1)

    def test_notification_failure_preserves_generated_image_save_state(self):
        job,_=self.bridge.enqueue(self.bridge.event(self.event()))
        with mock.patch('nextcloud_talk_hermes_bridge.secure_bridge.json_request',return_value=(200,{'artifact':job})), \
             mock.patch.object(self.bridge,'post',side_effect=OSError('offline')):
            self.bridge.run_one()
        with self.bridge.db() as db:
            row=db.execute('SELECT status,error FROM jobs WHERE id=?',(job,)).fetchone()
        self.assertEqual(row['status'],'awaiting_save')
        self.assertEqual(row['error'],'Notification_OSError')

    def test_discard_never_moves_or_deletes_the_user_image(self):
        runner=self.runner();artifact='a'*12
        with runner.db() as db:db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)',(artifact,'private-a','pending','AI Temporary/test.png',1))
        with mock.patch.object(runner.nc,'private_room',return_value=True),mock.patch.object(runner.nc,'move') as move:
            result=runner.execute({'uid':'alice','room':'private-a','job_id':'b'*12,'kind':'discard','artifact':artifact})
            self.assertEqual(result['artifact'],artifact);move.assert_not_called()

    def test_stdio_mcp_handshake_and_tool_schema_without_sdk_server(self):
        messages='\n'.join(json.dumps(m) for m in [{'jsonrpc':'2.0','id':1,'method':'initialize'},
                                                {'jsonrpc':'2.0','method':'notifications/initialized'},
                                                {'jsonrpc':'2.0','id':2,'method':'tools/list'}])+'\n'
        output=io.StringIO()
        with mock.patch('sys.stdin',io.StringIO(messages)),mock.patch('sys.stdout',output):secure_image_tool.main()
        replies=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[0]['result']['protocolVersion'],'2025-03-26')
        self.assertEqual(replies[1]['result']['tools'][0]['name'],'generate_image')

    def test_nextcloud_never_follows_redirects_or_accepts_traversal(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
        nc=Nextcloud('https://cloud.invalid','alice','test')
        with self.assertRaises(ValueError):nc.dav_url('../bob/private.png')
        with self.assertRaises(ValueError):Nextcloud('http://cloud.invalid','alice','test')

if __name__=='__main__':unittest.main()
