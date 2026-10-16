import json
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import test_feedback
from nextcloud_talk_hermes_bridge.assistant_delivery import Delivery
from nextcloud_talk_hermes_bridge.assistant_connector import Handler as ConnectorHandler
from nextcloud_talk_hermes_bridge.assistant_delivery import Handler as DeliveryHandler


class CompactBridgeTests(unittest.TestCase):
    setUp=test_feedback.FeedbackTests.setUp
    enqueue=test_feedback.FeedbackTests.enqueue
    row=test_feedback.FeedbackTests.row
    running=test_feedback.FeedbackTests.running
    def tracked(self,ident,room='main123',thread=0):
        with self.b.db() as db:
            job=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        with mock.patch.object(self.b,'feedback_request',return_value={'message_id':777}) as request:
            self.b.status_message(job,room,thread,'正在处理')
        return job

    def test_running_updates_one_receipt_then_final_replaces_it(self):
        ident=self.enqueue();self.running(ident)
        with mock.patch.object(self.b,'feedback_request',return_value={'message_id':777}) as request,mock.patch.object(self.b,'react'):
            self.b.flush_feedback(now=103);self.b.flush_feedback(now=103);self.b.flush_feedback(now=283)
            with self.b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM feedback_messages').fetchone()[0],1)
            job=self.row(ident);self.b.queue_message(job,0,'最终答案');self.b.finish_job(ident,'done')
            with mock.patch.object(self.b,'post') as post:self.b.flush_notification();post.assert_not_called()
            self.assertEqual(request.call_args.args[-2:],('finish','最终答案'))
            self.assertFalse(self.b.flush_feedback(now=9999999999))

    def test_choice_reuses_status_id_and_reactions_remain_on_the_choice(self):
        ident=self.enqueue();job=self.tracked(ident)
        self.b.queue_choice(job,0,'是否保存',{'✅':{'kind':'save','artifact':'b'*12}},'save:b')
        self.b.finish_job(ident,'done')
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'message_id':777})) as request,mock.patch.object(self.b,'react') as react:
            self.b.flush_notification()
            self.assertEqual(request.call_args.kwargs['body']['feedback_id'],ident)
            react.assert_called_once_with('main123',777,'✅')
        with self.b.db() as db:self.assertEqual(db.execute('SELECT message_id FROM choices').fetchone()[0],777)

    def test_lost_status_receipt_keeps_final_recoverable_and_no_model_retry(self):
        ident=self.enqueue();job=self.row(ident)
        with mock.patch.object(self.b,'feedback_request',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):self.b.status_message(job,'main123',0,'处理中')
        self.b.queue_message(job,0,'结果');self.b.finish_job(ident,'done')
        with mock.patch.object(self.b,'feedback_request',return_value={'closed':True}),mock.patch.object(self.b,'post') as post:
            self.b.flush_notification();post.assert_called_once()

    def test_cleanup_waits_for_result_delivery_and_only_targets_original_context(self):
        ident=self.enqueue();job=self.tracked(ident)
        self.b.queue_message(job,0,'结果',room='proj123',thread=9);self.b.finish_job(ident,'done')
        with mock.patch.object(self.b,'feedback_request') as request:
            self.assertFalse(self.b.flush_feedback(now=100));request.assert_not_called()
            with mock.patch.object(self.b,'post'):self.b.flush_notification()
            self.b.flush_feedback(now=101)
            self.assertEqual(request.call_args.args[1:4],('main123',0,'delete'))

    def test_failed_cleanup_is_bounded_and_does_not_reexecute_task(self):
        ident=self.enqueue();self.tracked(ident);self.b.finish_job(ident,'interrupted')
        with mock.patch.object(self.b,'feedback_request',side_effect=OSError) as request:
            for now in [100,101,130,160,190]:self.b.flush_feedback(now=now)
            self.assertEqual(request.call_count,3)
        self.assertEqual(self.row(ident)['status'],'interrupted')


class CompactDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.d=Delivery({'nextcloud_url':'https://cloud.invalid','assistant_uid':'helper','assistant_token':'synthetic','tenants':[]},self.tmp.name)
        self.tenant={'uid':'alice','rooms':['room123']}
        self.p={'room':'room123','thread':9,'id':'a'*12,'phase':'update','message':'处理中'}
        self.d.cloud.room=mock.Mock()
        self.message={'id':777,'actorType':'users','actorId':'helper','threadId':9}
        self.d.cloud.ocs=mock.Mock(side_effect=self.api)
        self.calls=[]

    def api(self,path,data=None,method='GET'):
        self.calls.append((path,data,method))
        return [self.message] if '/context?' in path else {'id':777}

    def test_same_message_updates_and_finishes_idempotently(self):
        self.assertEqual(self.d.feedback(self.tenant,self.p)['message_id'],777)
        self.d.feedback(self.tenant,{**self.p,'message':'还在处理'})
        self.d.feedback(self.tenant,{**self.p,'phase':'finish','message':'最终答案'})
        count=len(self.calls)
        self.d.feedback(self.tenant,{**self.p,'phase':'delete'})
        self.assertEqual(len(self.calls),count)
        self.assertEqual([c[2] for c in self.calls],['POST','GET','PUT','GET','PUT'])
        self.assertTrue(self.calls[0][1]['silent'])

    def test_foreign_uid_room_thread_and_actor_fail_closed(self):
        self.d.feedback(self.tenant,self.p)
        for tenant,p in [({'uid':'bob','rooms':['room123']},self.p),(self.tenant,{**self.p,'room':'room456'}),(self.tenant,{**self.p,'thread':10})]:
            with self.assertRaises(PermissionError):self.d.feedback(tenant,p)
        for message in [{**self.message,'actorId':'alice'},{**self.message,'actorType':'bots'},{**self.message,'threadId':10}]:
            self.message=message
            with self.assertRaises(PermissionError):self.d.feedback(self.tenant,{**self.p,'phase':'finish'})
        self.assertEqual([c[2] for c in self.calls].count('PUT'),0)

    def test_choice_card_edits_status_and_preserves_owner_and_thread(self):
        self.d.feedback(self.tenant,self.p)
        card={**self.p,'id':'b'*12,'feedback_id':'a'*12,'message':'是否保存'}
        result=self.d.question(self.tenant,card)
        self.assertEqual(result['message_id'],777)
        self.assertEqual(self.d.question(self.tenant,card),result)
        with self.assertRaises(PermissionError):self.d.question(self.tenant,{**card,'thread':10})

    def test_no_status_does_not_create_extra_completion_message(self):
        self.assertNotIn('message_id',self.d.feedback(self.tenant,{**self.p,'phase':'finish'}))
        self.assertFalse(self.calls)

    def test_delete_and_standalone_native_root_are_validated(self):
        self.p['thread']=0;self.message['threadId']=777
        self.d.feedback(self.tenant,self.p)
        self.d.feedback(self.tenant,{**self.p,'phase':'delete'})
        self.assertEqual(self.calls[-1][2],'DELETE')

    def test_worker_token_and_foreign_tenant_cannot_use_control_route(self):
        for token in ['worker','', 'other-tenant']:
            handler=object.__new__(ConnectorHandler);handler.path='/feedback'
            handler.server=SimpleNamespace(runner=SimpleNamespace(config={'runner_token':'bridge-control'}))
            handler.headers={'Authorization':'Bearer '+token};handler.reply=mock.Mock()
            handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],401)
        handler=object.__new__(DeliveryHandler);handler.path='/feedback'
        self.d.config['tenants']=[{**self.tenant,'token':'alice-delivery'},{'uid':'bob','rooms':['room456'],'token':'bob-delivery'}]
        handler.server=SimpleNamespace(runner=self.d)
        body=json.dumps(self.p).encode();handler.rfile=io.BytesIO(body)
        handler.headers={'Authorization':'Bearer bob-delivery','Content-Length':str(len(body))};handler.reply=mock.Mock()
        handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],403)


if __name__=='__main__':unittest.main()
