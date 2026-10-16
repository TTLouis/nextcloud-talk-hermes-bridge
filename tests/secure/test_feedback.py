import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge, Denied
from nextcloud_talk_hermes_bridge.secure_bridge import Handler


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'assistant_uid': 'helper', 'bot_secret': 'synthetic', 'bindings': [
            {'uid': 'alice', 'room': 'main123', 'runner_url': 'http://alice', 'runner_token': 'a'},
            {'uid': 'alice', 'room': 'proj123', 'runner_url': 'http://alice', 'runner_token': 'a'},
            {'uid': 'bob', 'room': 'main456', 'runner_url': 'http://bob', 'runner_token': 'b'}]}
        self.b = Bridge(self.config, self.root / 'jobs.sqlite')

    def enqueue(self, uid='alice', room='main123', mid=1, thread=0, message='work'):
        return self.b.enqueue((uid, room, mid, message, thread))[0]

    def row(self, ident):
        with self.b.db() as db:return db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()

    def running(self, ident, start=100):
        with self.b.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (ident,))
            db.execute('UPDATE task_feedback SET started=?,next_notice=? WHERE job_id=?',
                       (start, start + self.b.RUNNING_NOTICE_DELAY, ident))

    def test_received_is_native_reaction_deduplicated_across_restart_and_has_no_model_call(self):
        ident=self.enqueue()
        with mock.patch.object(self.b,'react') as react, mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request') as model:
            self.assertTrue(self.b.flush_feedback(now=100));self.assertFalse(self.b.flush_feedback(now=100))
            self.assertEqual(self.b.enqueue(('alice','main123',1,'work',0)),(ident,False))
            react.assert_called_once_with('main123',1,'👀',timeout=5);model.assert_not_called()
        restarted=Bridge(self.config,self.root/'jobs.sqlite')
        with mock.patch.object(restarted,'react') as react:
            self.assertFalse(restarted.flush_feedback(now=100));react.assert_not_called()

    def test_queued_and_running_notices_describe_actual_state_and_are_rate_limited(self):
        ident=self.enqueue()
        with self.b.db() as db:db.execute('UPDATE jobs SET created=100 WHERE id=?',(ident,))
        with mock.patch.object(self.b,'react'),mock.patch.object(self.b,'status_message') as post:
            self.b.flush_feedback(now=100)
            self.assertFalse(self.b.flush_feedback(now=114))
            self.b.flush_feedback(now=115)
            self.assertIn('排队',post.call_args.args[3]);self.assertFalse(self.b.flush_feedback(now=120))
            self.running(ident,130)
            self.assertFalse(self.b.flush_feedback(now=132))
            self.b.flush_feedback(now=133);self.assertIn('正在处理',post.call_args.args[3])
            self.assertFalse(self.b.flush_feedback(now=312))
            self.b.flush_feedback(now=313);self.assertIn('仍在处理',post.call_args.args[3])
            self.assertEqual(post.call_count,3)

    def test_two_users_and_project_thread_keep_distinct_status_destinations_without_quotes(self):
        alice=self.enqueue(room='proj123',thread=9);bob=self.enqueue(uid='bob',room='main456',mid=2)
        with mock.patch.object(self.b,'react') as react,mock.patch.object(self.b,'status_message') as post:
            self.b.flush_feedback(now=100);self.b.flush_feedback(now=100)
            self.assertEqual([c.args[:2] for c in react.call_args_list],[('proj123',1),('main456',2)])
            self.running(alice);self.running(bob)
            self.b.flush_feedback(now=103);self.b.flush_feedback(now=103)
            self.assertEqual([(c.args[1],c.args[2]) for c in post.call_args_list],[('proj123',9),('main456',0)])
            for call in post.call_args_list:
                self.assertNotIn('reply',call.kwargs);self.assertNotIn('work',call.args[3]);self.assertNotIn('bob',call.args[3])

    def test_other_users_binding_cannot_be_used_for_feedback(self):
        ident=self.enqueue()
        with self.b.db() as db:db.execute('UPDATE jobs SET reply_room=?,reply_thread=0 WHERE id=?',('main456',ident))
        with mock.patch.object(self.b,'react') as react,mock.patch.object(self.b,'status_message') as post:
            self.assertFalse(self.b.flush_feedback(now=9999999999));react.assert_not_called();post.assert_not_called()

    def test_mirrored_answer_acknowledges_its_real_card_room_and_resumes_exact_task(self):
        with self.b.db() as db:db.execute('INSERT INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('main123',99,'alice',0,'proj123',9,json.dumps({'1️⃣':{'kind':'answer','artifact':'a'*12,'decision':'A'}}),'answer:x','pending',None))
        ident,_=self.b.enqueue({'reaction':True,'uid':'alice','room':'main123','message_id':99,'emoji':'1️⃣'.replace('\ufe0f','')})
        self.assertEqual((self.row(ident)['room'],self.row(ident)['thread']),('proj123',9))
        with mock.patch.object(self.b,'react') as react,mock.patch.object(self.b,'status_message') as post:
            self.b.flush_feedback(now=100);react.assert_called_once_with('main123',99,'👀',timeout=5)
            self.running(ident);self.b.flush_feedback(now=103)
            self.assertEqual((post.call_args.args[1],post.call_args.args[2]),('main123',0))

    def test_status_emoji_never_resolves_a_choice_or_launches_a_task(self):
        with self.b.db() as db:db.execute('INSERT INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('main123',99,'alice',0,'main123',0,json.dumps({'✅':{'kind':'consent','decision':'allow'}}),'consent','pending',None))
        for actor in ['bots/1','users/helper','users/alice']:
            event=self.b.event({'type':'Like','actor':{'id':actor},'target':{'id':'main123'},'object':{'id':99},'content':'👀'})
            if event is not None:self.assertFalse(self.b.enqueue(event)[1])
        with self.b.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM choices').fetchone()[0],'pending')

    def test_missing_raw_guest_or_same_display_name_identity_cannot_acknowledge_or_enqueue(self):
        for actor in [{},{'name':'Alice'},{'id':'alice','name':'Alice'},{'id':'guests/alice','name':'Alice'},
                      {'id':'users/bob','name':'Alice'},['users/alice']]:
            payload={'type':'Create','actor':actor,'target':{'id':'main123'},'object':{'id':1,'type':'Note','content':json.dumps({'message':'work'})}}
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'projects':[{'room':'main456'}],'onboarding_complete':False})):
                with self.assertRaises(Denied):self.b.event(payload)
        with self.b.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_feedback').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_controls_and_onboarding_wait_do_not_report_model_running(self):
        self.enqueue(message='模型')
        task=self.enqueue(mid=2)
        with self.b.db() as db:db.execute("UPDATE jobs SET status='awaiting_onboarding' WHERE id=?",(task,))
        with mock.patch.object(self.b,'react'),mock.patch.object(self.b,'status_message') as post:
            self.b.flush_feedback(now=100);self.assertFalse(self.b.flush_feedback(now=9999999999));post.assert_not_called()
        with self.b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM task_feedback').fetchone()[0],1)

    def test_receipt_failure_has_bounded_retries_and_never_fails_or_reexecutes_a_job(self):
        ident=self.enqueue()
        with mock.patch.object(self.b,'react',side_effect=OSError('offline')) as react:
            for now in [100,101,130,160,190]:self.b.flush_feedback(now=now)
            self.assertEqual(react.call_count,3)
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'message':'done'})) as model:
            self.b.run_one();self.assertFalse(self.b.run_one());model.assert_called_once()
        self.assertEqual(self.row(ident)['status'],'done')

    def test_feedback_arrives_during_blocked_execution_with_one_global_job(self):
        alice=self.enqueue(room='proj123',thread=9);self.enqueue(uid='bob',room='main456',mid=2)
        entered=threading.Event();release=threading.Event();messages=[]
        def execute(*a,**kw):
            entered.set();release.wait(5);return 200,{'message':'done'}
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',side_effect=execute) as model, \
             mock.patch.object(self.b,'react'),mock.patch.object(self.b,'status_message',side_effect=lambda *a,**kw:messages.append((a,kw))):
            worker=threading.Thread(target=self.b.run_one);worker.start()
            try:
                self.assertTrue(entered.wait(2));self.assertFalse(self.b.run_one())
                with self.b.db() as db:db.execute('UPDATE task_feedback SET started=100,next_notice=103 WHERE job_id=?',(alice,))
                self.b.flush_feedback(now=103);self.b.flush_feedback(now=103);self.b.flush_feedback(now=103)
                self.assertTrue(any('正在处理' in a[3] and a[2]==9 for a,kw in messages))
                self.assertTrue(worker.is_alive());model.assert_called_once()
            finally:release.set();worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_terminal_failed_and_interrupted_jobs_never_emit_late_running_notices(self):
        ident=self.enqueue();self.running(ident)
        for state in ['done','failed','interrupted']:
            self.b.finish_job(ident,state)
            with mock.patch.object(self.b,'react') as react,mock.patch.object(self.b,'status_message') as post:
                self.assertFalse(self.b.flush_feedback(now=9999999999));react.assert_not_called();post.assert_not_called()

    def test_restart_does_not_resume_running_jobs_or_replay_their_notices(self):
        ident=self.enqueue();self.running(ident)
        restarted=Bridge(self.config,self.root/'jobs.sqlite')
        self.assertEqual(self.row(ident)['status'],'interrupted')
        with mock.patch.object(restarted,'react') as react,mock.patch.object(restarted,'post') as post:
            self.assertFalse(restarted.flush_feedback(now=9999999999));react.assert_not_called();post.assert_not_called()

    def test_unsigned_http_ingress_cannot_create_a_receipt_or_job(self):
        raw=json.dumps({'type':'Create','actor':{'id':'users/alice'},'target':{'id':'main123'},
                        'object':{'type':'Note','id':1,'content':json.dumps({'message':'work'})}}).encode()
        handler=object.__new__(Handler);handler.path='/hook';handler.server=SimpleNamespace(bridge=self.b)
        handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw);handler.reply=mock.Mock()
        handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],401)
        with self.b.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_feedback').fetchone()[0],0)

    def test_final_result_waits_for_inflight_status_then_no_late_status_is_sent(self):
        ident=self.enqueue();self.running(ident)
        with self.b.db() as db:db.execute('UPDATE task_feedback SET received=1 WHERE job_id=?',(ident,))
        sending=threading.Event();release=threading.Event();finished=threading.Event()
        def post(*a,**kw):sending.set();release.wait(5)
        def finish():self.b.finish_job(ident,'done');finished.set()
        with mock.patch.object(self.b,'status_message',side_effect=post):
            sender=threading.Thread(target=lambda:self.b.flush_feedback(now=103));sender.start()
            finalizer=threading.Thread(target=finish)
            try:
                self.assertTrue(sending.wait(2));finalizer.start()
                self.assertFalse(finished.wait(.05))
            finally:
                release.set();sender.join(2)
                if finalizer.ident is not None:finalizer.join(2)
        self.assertTrue(finished.is_set());self.assertEqual(self.row(ident)['status'],'done')
        with mock.patch.object(self.b,'status_message') as post:
            self.assertFalse(self.b.flush_feedback(now=9999999999));post.assert_not_called()


if __name__=='__main__':unittest.main()
