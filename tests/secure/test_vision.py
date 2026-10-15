import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge, Denied
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_context import task_workspace
from nextcloud_talk_hermes_bridge.assistant_vision import image_reference, IMAGE_LIMIT
from nextcloud_talk_hermes_bridge.assistant_worker import final_reply


class VisionContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.cfg={'uid':'alice','room':'room123','rooms':['room123'],'runner_token':'dispatch','worker_url':'http://worker',
            'worker_token':'worker','nextcloud_url':'https://cloud.invalid','nextcloud_token':'nc-secret',
            'assistant_uid':'hermes-assistant','model_url':'http://gateway/v1','model_token':'provider-secret',
            'chat_model':'gpt-6.1-sol','image_model':'gpt-image-2.5','delivery_url':'http://delivery','delivery_token':'delivery'}
        self.c=Connector(self.cfg,self.root/'control',self.root/'workspace')
        self.p={'uid':'alice','room':'room123','thread':12,'job_id':'a'*12,'message_id':20,'kind':'task','prompt':'看图','input_file':{'file_id':'99'}}
        self.native={'id':20,'actorType':'users','actorId':'alice','threadId':12,'messageParameters':{'file':{
            'type':'file','id':'99','path':'Talk/photo.png','mimetype':'image/png','size':'100','etag':'etag','hide-download':'no'}}}
        self.data=b'\x89PNG\r\n\x1a\n'+b'test-payload'

    def bridge(self):
        return Bridge({'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[
            {'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'token'}]},self.root/'bridge.sqlite')

    def event(self,typ='Activity',actor='users/alice',mime='image/png'):
        return {'type':typ,'actor':{'id':actor},'target':{'id':'room123'},'object':{'type':'Note','name':'file_shared','id':'20','threadId':12,
            'content':json.dumps({'message':'{file}','parameters':{'file':{'type':'file','id':'99','mimetype':mime,'link':'http://untrusted.invalid/image'}}})}}

    def test_native_create_and_activity_images_enqueue_once_in_exact_thread(self):
        b=self.bridge()
        first=b.enqueue(b.event(self.event()))
        self.assertTrue(first[1]);self.assertFalse(b.enqueue(b.event(self.event('Create')))[1])
        with b.db() as db:
            r=db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(r['thread'],12);self.assertEqual(json.loads(r['input_file']),{'file_id':'99'})
        self.assertNotIn('http',r['input_file'])

    def test_native_plain_text_with_empty_parameter_list_keeps_working(self):
        b=self.bridge();event=self.event('Create')
        event['object']['name']='message'
        event['object']['content']=json.dumps({'message':'继续刚才图片','parameters':[]})
        parsed=b.event(event)
        self.assertEqual(parsed,('alice','room123',20,'继续刚才图片',12))
        self.assertTrue(b.enqueue(parsed)[1])

    def test_helper_bot_and_nonimage_events_never_start_visual_task(self):
        b=self.bridge()
        for e in [self.event(actor='users/hermes-assistant'),self.event(actor='bots/1'),self.event(mime='application/pdf')]:
            self.assertIsNone(b.event(e))
        with b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_foreign_sender_and_malformed_image_identity_are_denied(self):
        b=self.bridge()
        with mock.patch.object(b,'refresh',side_effect=Denied('Unknown user')):
            with self.assertRaises(Denied):b.event(self.event(actor='users/bob'))
        for ident in ('../secret','https://bad','0','-1'):
            with self.assertRaises(ValueError):image_reference({'parameters':{'file':{'type':'file','id':ident,'mimetype':'image/png'}}})

    def test_direct_image_does_not_grant_drive_reads(self):
        work=task_workspace(self.c.workspace,'alice','room123',12)
        with mock.patch.object(self.c.nc,'ocs',return_value=[self.native]),mock.patch.object(self.c.nc,'download_message_image',return_value=self.data) as download:
            self.assertIsNone(self.c.consent())
            out=self.c.prepare_talk_image(self.p,work)
            self.assertEqual(out['path'],'incoming/'+self.p['job_id']+'.bin')
            self.assertEqual((work/'scratch'/out['path']).read_bytes(),self.data)
            download.assert_called_once_with('Talk/photo.png','99','etag',IMAGE_LIMIT)
            self.assertIsNone(self.c.consent())
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'list') as listing:
            with self.assertRaises(PermissionError):self.c.tool({'payload':self.p},'nextcloud_list',{})
            listing.assert_not_called()

    def test_image_source_requires_native_message_owner_room_thread_and_file_id(self):
        work=task_workspace(self.c.workspace,'alice','room123',12)
        for change in ({'actorId':'bob'},{'threadId':13},{'id':21},{'actorType':'bots'},
                       {'messageParameters':{'file':{**self.native['messageParameters']['file'],'id':'100'}}}):
            with mock.patch.object(self.c.nc,'ocs',return_value=[{**self.native,**change}]),mock.patch.object(self.c.nc,'download_message_image') as download:
                with self.assertRaises(PermissionError):self.c.prepare_talk_image(self.p,work)
                download.assert_not_called()

    def test_standalone_rest_self_thread_maps_to_signed_main_context(self):
        p={**self.p,'thread':0}
        work=task_workspace(self.c.workspace,'alice','room123',0)
        with mock.patch.object(self.c.nc,'ocs',return_value=[{**self.native,'threadId':20}]),mock.patch.object(self.c.nc,'download_message_image',return_value=self.data):
            self.assertIsNotNone(self.c.prepare_talk_image(p,work))

    def test_omitted_webhook_thread_is_resolved_from_owned_native_message(self):
        with mock.patch.object(self.c.nc,'ocs',return_value=[self.native]):
            self.assertEqual(self.c.resolve_native_thread({**self.p,'thread':0}),12)
            with self.assertRaises(PermissionError):self.c.resolve_native_thread({**self.p,'thread':13})
        with mock.patch.object(self.c.nc,'ocs',return_value=[{**self.native,'actorId':'bob'}]):
            with self.assertRaises(PermissionError):self.c.resolve_native_thread({**self.p,'thread':0})

    def test_native_root_title_distinguishes_task_start_from_main_conversation(self):
        root={**self.native,'threadId':20}
        with mock.patch.object(self.c.nc,'ocs',side_effect=[[root],{'thread':{'title':''}}]):
            self.assertEqual(self.c.resolve_native_thread({**self.p,'thread':0}),0)
        with mock.patch.object(self.c.nc,'ocs',side_effect=[[root],{'thread':{'title':'预算任务'}}]):
            self.assertEqual(self.c.resolve_native_thread({**self.p,'thread':0}),20)

    def test_native_context_is_applied_before_worker_and_model_receipt(self):
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'ocs',return_value={'thread':{'title':'预算任务'}}):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with mock.patch.object(self.c,'resolve_native_thread',return_value=12),mock.patch.object(self.c,'prepare_talk_image',return_value=None),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'message':'收到'})) as worker:
                result=self.c.execute({**self.p,'thread':0,'native_input':True})
                self.assertEqual(result['execution_thread'],12)
                self.assertEqual(worker.call_args.kwargs['body']['thread'],12)
                with self.c.db() as db:self.assertEqual(db.execute('SELECT thread FROM task_models').fetchone()[0],12)

    def test_native_missing_image_context_is_permission_denial(self):
        import urllib.error
        work=task_workspace(self.c.workspace,'alice','room123',12)
        for status in (304,403,404):
            with mock.patch.object(self.c.nc,'ocs',side_effect=urllib.error.HTTPError('url',status,'Unavailable',{},None)):
                with self.assertRaises(PermissionError):self.c.prepare_talk_image(self.p,work)

    def test_protected_oversized_or_fake_images_are_not_given_to_worker(self):
        work=task_workspace(self.c.workspace,'alice','room123',12)
        for changes in ({'hide-download':'yes'},{'size':str(IMAGE_LIMIT+1)}):
            message={**self.native,'messageParameters':{'file':{**self.native['messageParameters']['file'],**changes}}}
            with mock.patch.object(self.c.nc,'ocs',return_value=[message]),mock.patch.object(self.c.nc,'download_message_image') as download:
                with self.assertRaises((ValueError,PermissionError)):self.c.prepare_talk_image(self.p,work)
                download.assert_not_called()
        with mock.patch.object(self.c.nc,'ocs',return_value=[self.native]),mock.patch.object(self.c.nc,'download_message_image',return_value=b'<svg>bad</svg>'):
            with self.assertRaises(ValueError):self.c.prepare_talk_image(self.p,work)

    def test_dav_file_identity_and_etag_prevent_replacement_or_url_fetch(self):
        xml=b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:response><d:propstat><d:prop><oc:fileid>99</oc:fileid><d:getetag>"etag"</d:getetag><d:resourcetype/></d:prop></d:propstat></d:response></d:multistatus>'
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_cloud.request',side_effect=[(207,xml),(200,self.data)]) as req:
            self.assertEqual(self.c.nc.download_message_image('Talk/photo.png','99','etag',IMAGE_LIMIT),self.data)
            self.assertEqual(req.call_args.kwargs['headers']['If-Match'],'"etag"')
            self.assertTrue(req.call_args.args[0].startswith('https://cloud.invalid/remote.php/dav/files/alice/'))
        for ident,etag in [('100','etag'),('99','new')]:
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_cloud.request',return_value=(207,xml)) as req:
                with self.assertRaises(PermissionError):self.c.nc.download_message_image('Talk/photo.png',ident,etag,IMAGE_LIMIT)
                self.assertEqual(req.call_count,1)

    def test_native_query_parameters_are_not_corrupted_by_json_format(self):
        with mock.patch('nextcloud_talk_hermes_bridge.secure_common.json_request',return_value=(200,{'ocs':{'meta':{'statuscode':200},'data':[]}})) as req:
            self.c.nc.ocs('apps/spreed/api/v1/chat/room123/20/context?limit=0&threadId=12')
            url=req.call_args.args[0]
            self.assertEqual(url.count('?'),1);self.assertIn('threadId=12&format=json',url)

    def test_invalid_image_does_not_start_worker_or_consume_model_override(self):
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'ocs',return_value={'thread':{'title':'任务'}}):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with mock.patch.object(self.c,'prepare_talk_image',side_effect=PermissionError('Wrong image')),mock.patch.object(self.c,'assign_task_model') as model,mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request') as worker:
                with self.assertRaises(PermissionError):self.c.execute(self.p)
                model.assert_not_called();worker.assert_not_called()

    def test_partial_final_text_is_delivered_with_incomplete_outcome(self):
        work=task_workspace(self.c.workspace,'alice','room123',12)
        raw='已完成两步；下一步受工具预算限制，尚未完成。'
        r=final_reply(self.c.workspace,work,self.p,{'text':raw,'exit_code':1},1)
        self.assertEqual(r['message'],raw);self.assertEqual(r['outcome'],'incomplete')
        with self.assertRaises(RuntimeError):final_reply(self.c.workspace,work,self.p,{'text':'','exit_code':1},1)

    def test_bridge_reports_partial_result_without_marking_job_done_or_retrying(self):
        b=self.bridge();ident,_=b.enqueue(('alice','room123',20,'test',12))
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'message':'当前进度','outcome':'incomplete'})) as worker:
            self.assertTrue(b.run_one());self.assertFalse(b.run_one());self.assertEqual(worker.call_count,1)
        with b.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(ident,)).fetchone()[0],'incomplete')
            self.assertIn('尚未完成',db.execute('SELECT message FROM notifications').fetchone()[0])

    def test_only_related_handoff_results_enter_coordination_context(self):
        with self.c.db() as db:
            db.execute('INSERT INTO projects VALUES (?,?)',('room456','项目 · 预算'))
            for ident,source,thread,target in [('b'*12,'room123',12,25),('c'*12,'room123',13,26),('d'*12,'room789',0,27)]:
                db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?,?,?)',(ident,source,thread,'room456',target,'PRIVATE RAW CONTEXT','预算任务'))
                db.execute('INSERT INTO task_state VALUES (?,?,?,?,?)',('room456',target,'incomplete','部分完成，待确认',100))
        with mock.patch.object(self.c.nc,'room'):
            brief=self.c.coordination_brief(self.p)
            self.assertEqual(len(brief),1);self.assertEqual(brief[0]['thread'],25)
            self.assertEqual(brief[0]['status'],'incomplete');self.assertNotIn('PRIVATE',json.dumps(brief))
        with mock.patch.object(self.c.nc,'room',side_effect=PermissionError('Removed')):
            self.assertEqual(self.c.coordination_brief(self.p),[])

    def test_proxy_context_metadata_is_server_owned_and_audit_has_no_content(self):
        a={'payload':{**self.p,'chat_model':'gpt-6.1-sol'},'models':1}
        raw,seq=self.c.model_envelope(a,{'model':'gpt-6.1-sol','metadata':{'hermes_context':'bob'},'messages':[{'content':'private image or message'}]},'/v1/chat/completions')
        meta=json.loads(raw)['metadata'];self.assertNotEqual(meta['hermes_context'],'bob')
        self.c.finish_model_audit(self.p['job_id'],seq,200)
        with self.c.db() as db:
            row=dict(db.execute('SELECT * FROM model_audit').fetchone())
            self.assertEqual(row['status'],200);self.assertNotIn('private image',json.dumps(row))
            self.assertNotIn('nc-secret',json.dumps(row))

    def test_parallel_model_requests_keep_distinct_audit_receipts(self):
        from concurrent.futures import ThreadPoolExecutor
        active={'payload':{**self.p,'chat_model':'gpt-6.1-sol'},'models':8}
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts=list(pool.map(lambda _:self.c.model_envelope(active,{'model':'gpt-6.1-sol'},'/v1/chat/completions'),range(8)))
        self.assertEqual(sorted(seq for _,seq in receipts),list(range(1,9)))


if __name__=='__main__':unittest.main()
