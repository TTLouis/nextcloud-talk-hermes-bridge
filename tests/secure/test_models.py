import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge,Denied
from nextcloud_talk_hermes_bridge.assistant_worker import Worker

SOL='gpt-6.1-sol'
TERRA='gpt-5.6-terra'

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.cfg={'uid':'alice','room':'main123','rooms':['main123','proj123'],'runner_token':'dispatch',
            'worker_url':'http://worker','worker_token':'worker','nextcloud_url':'https://cloud.invalid','nextcloud_token':'nc',
            'assistant_uid':'assistant','chat_model':SOL,'allowed_chat_models':[SOL,TERRA]}
        self.c=Connector(self.cfg,self.root/'control',self.root/'work');self.counter=0

    def p(self,room='main123',thread=0,kind='task',**extra):
        self.counter+=1
        return {'uid':'alice','room':room,'thread':thread,'job_id':format(self.counter,'012x'),'kind':kind,'prompt':'calculate',**extra}

    def call(self,p):
        with mock.patch.object(self.c.nc,'room',return_value={'displayName':'main'}):return self.c.execute(p)

    def choose(self,scope,model=None,room='main123',thread=0):
        card=self.call(self.p(room,thread,'model_menu'))['model_card']
        stage=self.call(self.p(room,thread,'model_scope',artifact=card['id'],decision=scope))
        if model is None:return stage
        return self.call(self.p(room,thread,'model_select',artifact=stage['model_card']['id'],decision=model))

    def bridge(self):
        return Bridge({'assistant_uid':'assistant','bot_secret':'secret','bindings':[{'uid':'alice','room':'main123','runner_url':'http://connector','runner_token':'dispatch'},{'uid':'alice','room':'proj123','runner_url':'http://connector','runner_token':'dispatch'}]},self.root/'bridge.sqlite')

    def test_user_default_persists_and_new_user_has_separate_default(self):
        self.choose('default',TERRA)
        restarted=Connector(self.cfg,self.root/'control',self.root/'work')
        self.assertEqual(restarted.model_status(self.p())[0],TERRA)
        bob=Connector({**self.cfg,'uid':'bob'},self.root/'bob-control',self.root/'bob-work')
        self.assertEqual(bob.model_status({'room':'main123','thread':0})[0],SOL)
        self.assertIsNone(self.c.consent());self.assertIsNone(bob.consent())

    def test_once_override_is_context_bound_consumed_exactly_once_and_replay_is_stable(self):
        self.choose('once',TERRA)
        other=self.c.assign_task_model(self.p('proj123',4));self.assertEqual(other['model'],SOL)
        first=self.p();receipt=self.c.assign_task_model(first)
        self.assertEqual((receipt['model'],receipt['source']),(TERRA,'once'))
        replay=self.c.assign_task_model(first);self.assertEqual(replay['model'],TERRA)
        self.assertEqual(self.c.assign_task_model(self.p())['model'],SOL)
        self.assertIsNone(self.c.model_status(first)[1])

    def test_controls_and_incomplete_onboarding_do_not_consume_once(self):
        self.choose('once',TERRA)
        result=self.call(self.p());self.assertTrue(result['requires_consent'])
        self.call(self.p(kind='consent',decision='deny'))
        self.assertEqual(self.c.model_status(self.p())[1],TERRA)
        with mock.patch.object(self.c.nc,'ocs'),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'message':'42'})) as worker:
            result=self.call(self.p());self.assertEqual(result['model'],TERRA)
            self.assertEqual(worker.call_args.kwargs['body']['chat_model'],TERRA)
        self.assertIs(self.c.consent(),False)

    def test_frozen_model_does_not_follow_later_default_changes(self):
        p=self.p();receipt=self.c.assign_task_model(p)
        self.choose('default',TERRA)
        self.assertEqual(self.c.assign_task_model(p)['model'],SOL)
        active={'payload':{'chat_model':receipt['model']}}
        self.c.check_task_model(active,{'model':SOL})
        with self.assertRaises(PermissionError):self.c.check_task_model(active,{'model':TERRA})
        self.assertEqual(self.c.assign_task_model(self.p())['model'],TERRA)

    def test_answer_and_handoff_inherit_original_model_without_consuming_other_override(self):
        self.choose('once',TERRA);parent=self.p();self.c.assign_task_model(parent)
        self.choose('once',SOL)
        continued=self.c.assign_task_model(self.p(),question_origin=parent['job_id'],is_answer=True)
        self.assertEqual((continued['model'],continued['root_job']),(TERRA,parent['job_id']))
        self.assertEqual(self.c.model_status(parent)[1],SOL)
        with self.c.db() as db:db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?,?,?)',('a'*12,'main123',0,'proj123',9,'context','Task'))
        child=self.p('proj123',9,inherit_model_from=parent['job_id'],handoff_id='a'*12)
        self.assertEqual(self.c.assign_task_model(child)['model'],TERRA)
        self.assertEqual(self.c.model_status(parent)[1],SOL)

    def test_foreign_origin_or_receipt_cannot_select_a_model(self):
        parent=self.p();self.c.assign_task_model(parent)
        with self.assertRaises(PermissionError):self.c.assign_task_model(self.p('proj123',5),question_origin=parent['job_id'],is_answer=True)
        with self.assertRaises(PermissionError):self.c.assign_task_model(self.p('proj123',5,inherit_model_from=parent['job_id'],handoff_id='b'*12))
        with self.assertRaises(PermissionError):self.c.assign_task_model({**parent,'room':'proj123'})

    def test_model_card_cannot_be_forged_replayed_or_cross_thread(self):
        card=self.call(self.p(kind='model_menu'))['model_card']
        for p in [self.p(kind='model_scope',artifact=card['id'],decision='evil'),self.p(thread=8,kind='model_scope',artifact=card['id'],decision='default'),self.p(kind='model_scope',artifact=card['id'],decision='default',uid='bob')]:
            with self.assertRaises(PermissionError):self.call(p)
        stage=self.call(self.p(kind='model_scope',artifact=card['id'],decision='default'))['model_card']
        with self.assertRaises(PermissionError):self.call(self.p(kind='model_select',artifact=stage['id'],decision='unapproved'))
        self.call(self.p(kind='model_select',artifact=stage['id'],decision=TERRA))
        with self.assertRaises(PermissionError):self.call(self.p(kind='model_select',artifact=stage['id'],decision=SOL))
        self.assertEqual(self.c.model_status(self.p())[0],TERRA)

    def test_new_menu_supersedes_old_menu_and_cancel_clear_leave_default_intact(self):
        old=self.call(self.p(kind='model_menu'))['model_card']
        self.call(self.p(kind='model_menu'))
        with self.assertRaises(PermissionError):self.call(self.p(kind='model_scope',artifact=old['id'],decision='once'))
        self.choose('once',TERRA);self.choose('cancel')
        self.assertEqual(self.c.model_status(self.p())[1],TERRA)
        self.choose('clear');self.assertEqual(self.c.model_status(self.p()),(SOL,None))

    def test_worker_rejects_wrong_user_or_unapproved_model_before_start(self):
        w=Worker(self.cfg,self.root/'worker')
        p=self.p(grant='a'*64,chat_model=SOL)
        with mock.patch.object(w,'run',return_value={'message':'ok'}) as run:
            w.execute(p);self.assertEqual(run.call_count,1)
            for bad in [{**p,'uid':'bob'},{**p,'chat_model':'evil'},{**p,'chat_model':None}]:
                with self.assertRaises(PermissionError):w.execute(bad)
            self.assertEqual(run.call_count,1)

    def test_main_messages_do_not_quote_but_project_messages_stay_in_their_thread(self):
        b=self.bridge();main,_=b.enqueue(('alice','main123',1,'hello',0));task,_=b.enqueue(('alice','proj123',2,'hello',9))
        with b.db() as db:jobs=[db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone() for ident in [main,task]]
        b.queue_message(jobs[0],0,'answer',reply=1);b.queue_message(jobs[1],0,'answer',reply=2)
        with b.db() as db:rows=db.execute('SELECT room,reply,thread FROM notifications ORDER BY rowid').fetchall()
        self.assertEqual([tuple(r) for r in rows],[('main123',0,0),('proj123',0,9)])

    def test_model_commands_and_model_cards_are_deterministic_controls(self):
        b=self.bridge();ident,_=b.enqueue(('alice','main123',1,'/model',0))
        with b.db() as db:job=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        self.assertEqual(job['kind'],'model_menu')
        card=self.call(self.p(kind='model_menu'))['model_card'];b.queue_model_card(job,0,card)
        with b.db() as db:row=db.execute('SELECT reply,thread,choice FROM notifications').fetchone()
        self.assertEqual((row['reply'],row['thread']),(0,0))
        self.assertEqual(json.loads(row['choice'])['options']['1️⃣']['kind'],'model_scope')

    def test_proactive_welcome_prevents_duplicate_consent_cards_and_keeps_request(self):
        b=self.bridge()
        welcome={'id':'f'*12,'uid':'alice','room':'main123','thread':0,'reply_room':None,'reply_thread':None}
        b.queue_choice(welcome,0,'Welcome',{'✅':{'kind':'consent','decision':'allow'},'❌':{'kind':'consent','decision':'deny'}},'consent')
        self.assertTrue(b.has_pending_consent('alice'));self.assertFalse(b.has_pending_consent('bob'))
        ident,_=b.enqueue(('alice','main123',1,'real first request',0))
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'requires_consent':True,'message':'Welcome'})):
            b.run_one()
        with b.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(ident,)).fetchone()[0],'awaiting_onboarding')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notifications WHERE choice IS NOT NULL').fetchone()[0],1)
        self.assertEqual(b.release_onboarding('alice'),1)
        self.assertFalse(b.has_pending_consent('alice'))

if __name__=='__main__':unittest.main()
