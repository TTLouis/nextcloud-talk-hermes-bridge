import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from nextcloud_talk_hermes_bridge.assistant_context import context_key,relative_path,read_workspace,write_workspace,task_workspace
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge,Denied

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.config={'uid':'alice','room':'room123','rooms':['room123'],'runner_token':'dispatch','worker_url':'http://worker:8790',
            'worker_token':'worker','nextcloud_url':'https://cloud.invalid','nextcloud_token':'nc-secret',
            'assistant_uid':'hermes-assistant','model_url':'http://gateway/v1','model_token':'provider-secret',
            'chat_model':'gpt-5.6-terra','image_model':'gpt-image-2.5','delivery_url':'http://delivery:8792','delivery_token':'delivery'}
        self.c=Connector(self.config,self.root/'control',self.root/'workspace')
        self.p={'uid':'alice','room':'room123','thread':12,'job_id':'a'*12,'kind':'task','prompt':'hello'}

    def test_context_isolated_by_user_project_and_real_thread(self):
        keys=[context_key('alice','room123',12),context_key('bob','room123',12),context_key('alice','room456',12),context_key('alice','room123',13)]
        self.assertEqual(len(set(keys)),4)
        self.assertNotEqual(context_key('alice','room123',0),keys[0])
        for v in [-1,'12',True]:
            with self.assertRaises(ValueError):context_key('alice','room123',v)

    def test_cloud_read_requires_explicit_consent_and_remains_revocable(self):
        a={'payload':self.p}
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'list') as listing:
            with self.assertRaises(PermissionError):self.c.tool(a,'nextcloud_list',{})
            listing.assert_not_called()
            self.c.execute({**self.p,'kind':'consent','decision':'allow'})
            self.c.tool(a,'nextcloud_list',{})
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with self.assertRaises(PermissionError):self.c.tool(a,'nextcloud_list',{})
            self.assertEqual(listing.call_count,1)

    def test_first_task_asks_consent_without_launching_hermes(self):
        with mock.patch.object(self.c.nc,'room'),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request') as req:
            answer=self.c.execute(self.p)
            self.assertTrue(answer['requires_consent']);req.assert_not_called()
            for phrase in ['个人在线助理','直接告诉我','Docker 私有工作区','暂不授权仍可','你的请求已经保留']:
                self.assertIn(phrase,answer['message'])

    def test_connector_rejects_foreign_user_room_and_save_thread(self):
        for p in [{**self.p,'uid':'bob'},{**self.p,'room':'room456'}]:
            with self.assertRaises(PermissionError):self.c.execute(p)
        with self.c.db() as db:db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)',('b'*12,'room123',13,'test.txt','pending','hash',None,None))
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'upload_file') as upload:
            with self.assertRaises(PermissionError):self.c.execute({**self.p,'kind':'save','artifact':'b'*12})
            upload.assert_not_called()

    def test_normal_response_is_returned_and_grant_expires_at_end(self):
        with mock.patch.object(self.c.nc,'room'):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with mock.patch.object(self.c.nc,'ocs',return_value={'thread':{'name':'test'}}),mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'message':'The answer is 42'})):
                result=self.c.execute(self.p)
                self.assertEqual(result['message'],'The answer is 42');self.assertEqual(result['artifacts'],[])
        with self.assertRaises(PermissionError):self.c.active_grant('Bearer anything')

    def test_save_uploads_owned_snapshot_only_after_explicit_action(self):
        artifact='b'*12;(self.c.state/'artifacts'/artifact).write_bytes(b'result')
        with self.c.db() as db:db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)',(artifact,'room123',12,'test.txt','pending','hash',None,None))
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'mkdirs'),mock.patch.object(self.c.nc,'upload_file') as upload:
            self.c.execute({**self.p,'kind':'save','artifact':artifact})
            upload.assert_called_once_with('AI Outputs/room123/'+artifact+'-test.txt',b'result')
            self.c.execute({**self.p,'kind':'save','artifact':artifact});self.assertEqual(upload.call_count,1)

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'Linux container filesystem contract')
    def test_symlinks_in_any_workspace_component_cannot_expose_connector_secret(self):
        workspace=self.c.workspace;outside=self.c.state/'secret';outside.write_text('not-for-model')
        (workspace/'link').symlink_to(self.c.state,target_is_directory=True)
        for action in [lambda:read_workspace(workspace,'link/secret'),lambda:write_workspace(workspace,'link/new',b'bad')]:
            with self.assertRaises(OSError):action()
        self.assertFalse((self.c.state/'new').exists())
        (workspace/'projects').symlink_to(self.c.state,target_is_directory=True)
        with self.assertRaises(OSError):task_workspace(workspace,'alice','room123',12)

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'Linux container filesystem contract')
    def test_present_file_uses_assistant_delivery_and_never_user_upload(self):
        work=task_workspace(self.c.workspace,'alice','room123',12)
        write_workspace(self.c.workspace,str(work.relative_to(self.c.workspace))+'/scratch/out.txt',b'answer')
        active={'payload':self.p,'work':work,'artifacts':[]};self.c.active=active
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'upload_file') as upload, \
            mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'share_id':1})) as deliver:
            result=self.c.tool(active,'present_file',{'path':'out.txt'})
            upload.assert_not_called();self.assertFalse(result['saved_to_user_drive'])
            self.assertEqual(deliver.call_args.kwargs['body']['thread'],12)

    def test_paths_reject_traversal_absolute_and_backslash(self):
        for path in ['../secret','/secret','a/../b','a//b','a\\b','a/./b']:
            with self.assertRaises(ValueError):relative_path(path)

    def test_signed_event_routes_real_threads_and_does_not_use_display_names(self):
        cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[{'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'}]}
        b=Bridge(cfg,self.root/'bridge.sqlite')
        def event(mid,thread):return {'type':'Create','actor':{'id':'users/alice'},'target':{'id':'room123'},'object':{'type':'Note','id':str(mid),'threadId':thread,'content':json.dumps({'message':'hello'})}}
        a,_=b.enqueue(b.event(event(1,12)));second,_=b.enqueue(b.event(event(2,13)))
        with b.db() as db:threads=[r['thread'] for r in db.execute('SELECT thread FROM jobs ORDER BY message_id')]
        self.assertEqual(threads,[12,13]);self.assertNotEqual(a,second)
        self.assertEqual(b.enqueue(b.event(event(1,12))),(a,False))
        with self.assertRaises(Denied):b.event(event(3,'12'))

    def test_project_proposal_cannot_be_confirmed_from_another_task(self):
        with self.c.db() as db:db.execute('INSERT INTO proposals VALUES (?,?,?,?,?)',('b'*12,'room123',13,json.dumps({'kind':'rename','name':'new'}),'pending'))
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c,'project_change') as change:
            with self.assertRaises(PermissionError):self.c.execute({**self.p,'kind':'confirm','artifact':'b'*12})
            change.assert_not_called()

    def test_failed_notification_retries_delivery_without_reexecuting_agent(self):
        cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[{'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'}]}
        b=Bridge(cfg,self.root/'notifications.sqlite')
        b.enqueue(('alice','room123',1,'hello',12))
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'message':'answer','artifacts':[{'artifact':'a'*12,'name':'out.txt'}]})) as execute:
            b.run_one()
            with mock.patch.object(b,'post',side_effect=OSError('offline')):b.flush_notification()
            self.assertEqual(execute.call_count,1)
        with b.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'done')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0],2)

    def test_revocation_uses_immediate_control_path_before_queue(self):
        cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[{'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'}]}
        b=Bridge(cfg,self.root/'revoke.sqlite')
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'revoked':True})) as call:
            b.enqueue(('alice','room123',1,'撤销网盘授权',12))
            self.assertEqual(call.call_args.args[0],'http://connector/consent')
            self.assertEqual(call.call_args.kwargs['body']['uid'],'alice')

    def test_project_task_listing_strips_every_other_tasks_chat_text(self):
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'ocs',return_value=[{'thread':{'id':12,'name':'Task A'},'first':{'message':'private A'},'last':{'message':'private B'}}]):
            result=self.c.tool({'payload':self.p},'project_tasks',{})
            self.assertEqual(result['tasks'],[{'id':12,'name':'Task A'}])
            self.assertNotIn('private',json.dumps(result))

    def test_main_conversation_runs_agent_without_requiring_manual_task_command(self):
        with mock.patch.object(self.c.nc,'room',return_value={'displayName':'Main'}):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'message':'Hello from your assistant'})) as call:
                result=self.c.execute({**self.p,'thread':0})
                self.assertEqual(result['message'],'Hello from your assistant')
                self.assertTrue(call.call_args.kwargs['body']['coordination'])

    def test_dedicated_task_cannot_recursively_handoff_to_new_projects(self):
        with self.assertRaises(PermissionError):self.c.handoff({'payload':self.p},{})

    def test_handoff_rejects_other_users_project_binding(self):
        p={**self.p,'thread':0};active={'payload':p,'handoffs':[]};self.c.active=active
        args={'name':'Project','task_title':'Task','goal':'Goal','context':'Context','next_steps':['Next'],'project_room':'foreign-room'}
        with self.assertRaises(PermissionError):self.c.handoff(active,args)

    def test_handoff_jobs_keep_owner_and_parent_but_do_not_copy_main_session(self):
        cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[
            {'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'},
            {'uid':'alice','room':'room456','runner_url':'http://connector','runner_token':'x'}]}
        b=Bridge(cfg,self.root/'handoff.sqlite');parent,_=b.enqueue(('alice','room123',1,'Please handle this',0))
        with b.db() as db:job=db.execute('SELECT * FROM jobs WHERE id=?',(parent,)).fetchone()
        h={'id':'b'*12,'source_room':'room123','source_thread':0,'target_room':'room456','target_thread':20,'prompt':'Relevant handoff only','title':'Task'}
        child=b.enqueue_handoff(job,h)
        with b.db() as db:r=db.execute('SELECT * FROM jobs WHERE id=?',(child,)).fetchone()
        self.assertEqual((r['uid'],r['room'],r['thread'],r['parent_room']),('alice','room456',20,'room123'))
        self.assertNotEqual(context_key('alice','room123',0),context_key(r['uid'],r['room'],r['thread']))

    def choice_bridge(self):
        cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[
            {'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'},
            {'uid':'alice','room':'room456','runner_url':'http://connector','runner_token':'x'}]}
        return Bridge(cfg,self.root/'choice-bridge.sqlite')

    def add_choice(self,b,room,mid,target='room456',thread=20,group='save:artifact'):
        options={'✅':{'kind':'save','artifact':'a'*12},'❌':{'kind':'discard','artifact':'a'*12}}
        with b.db() as db:db.execute('INSERT INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)',(room,mid,'alice',0,target,thread,json.dumps(options),group,'pending',None))

    def test_reaction_saves_only_bound_output_and_main_card_routes_to_project(self):
        b=self.choice_bridge();self.add_choice(b,'room123',100)
        job,new=b.enqueue({'reaction':True,'uid':'alice','room':'room123','message_id':100,'emoji':'✅'})
        self.assertTrue(new)
        with b.db() as db:r=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
        self.assertEqual((r['kind'],r['room'],r['thread'],r['reply_room']),('save','room456',20,'room123'))

    def test_reaction_cannot_approve_another_users_question(self):
        b=self.choice_bridge();self.add_choice(b,'room123',100)
        with self.assertRaises(Denied):b.enqueue({'reaction':True,'uid':'bob','room':'room123','message_id':100,'emoji':'✅'})
        with b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_mirrored_cards_and_repeated_reactions_execute_once(self):
        b=self.choice_bridge();self.add_choice(b,'room123',100);self.add_choice(b,'room456',101)
        first=b.enqueue({'reaction':True,'uid':'alice','room':'room123','message_id':100,'emoji':'✅'})
        second=b.enqueue({'reaction':True,'uid':'alice','room':'room456','message_id':101,'emoji':'❌'})
        self.assertTrue(first[1]);self.assertFalse(second[1]);self.assertEqual(first[0],second[0])
        with b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_unrelated_emoji_and_removing_reaction_never_execute(self):
        b=self.choice_bridge();self.add_choice(b,'room123',100)
        self.assertFalse(b.enqueue({'reaction':True,'uid':'alice','room':'room123','message_id':100,'emoji':'🎉'})[1])
        self.assertIsNone(b.event({'type':'Undo','actor':{'id':'users/alice'},'target':{'id':'room123'}}))
        with b.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_native_like_payload_uses_reacting_owner_not_message_author(self):
        b=self.choice_bridge();self.add_choice(b,'room123',100)
        payload={'type':'Like','actor':{'id':'users/alice'},'target':{'id':'room123'},'object':{'id':'100','type':'Note'},'content':'✅'}
        e=b.event(payload);self.assertEqual(e['uid'],'alice');self.assertTrue(b.enqueue(e)[1])

    def test_answer_card_does_not_grant_cloud_permission_or_save_files(self):
        with self.c.db() as db:db.execute('INSERT INTO questions VALUES (?,?,?,?,?,?,?)',('b'*12,'room123',12,'Choose one',json.dumps(['允许读取并保存','Option B']),'pending',None))
        with mock.patch.object(self.c.nc,'room'),mock.patch.object(self.c.nc,'upload_file') as upload:
            result=self.c.execute({**self.p,'kind':'answer','artifact':'b'*12,'decision':'允许读取并保存'})
            self.assertTrue(result['requires_consent']);self.assertIsNone(self.c.consent());upload.assert_not_called()

    def test_first_request_survives_restart_and_allow_or_decline_resumes_exact_context(self):
        for decision in ['allow','deny']:
            with self.subTest(decision=decision):
                cfg={'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[{'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'x'}]}
                path=self.root/(decision+'.sqlite');b=Bridge(cfg,path)
                source,_=b.enqueue(('alice','room123',10,'Please compute 17+25',12))
                with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'requires_consent':True,'message':'welcome'})):
                    b.run_one()
                with b.db() as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(source,)).fetchone()[0],'awaiting_onboarding')
                b=Bridge(cfg,path)
                with b.db() as db:
                    db.execute('INSERT INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)',('room123',100,'alice',12,'room123',12,json.dumps({'✅':{'kind':'consent','decision':decision}}),'consent','pending',None))
                with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'revoked':True})):
                    b.enqueue({'reaction':True,'uid':'alice','room':'room123','message_id':100,'emoji':'✅'})
                replies=[(200,{'onboarding_complete':True,'message':'choice recorded'}),(200,{'message':'42'})]
                with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',side_effect=replies) as req:
                    b.run_one();b.run_one()
                    resumed=req.call_args_list[-1].kwargs['body']
                    self.assertEqual((resumed['uid'],resumed['room'],resumed['thread'],resumed['prompt'],resumed['job_id']),('alice','room123',12,'Please compute 17+25',source))
                with b.db() as db:
                    self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(source,)).fetchone()[0],'done')
                    self.assertEqual(db.execute('SELECT message FROM notifications WHERE id=?',(source+'-0',)).fetchone()[0],'42')

    def test_repeated_messages_before_first_choice_keep_one_welcome_card(self):
        b=self.choice_bridge()
        for mid in [10,11]:b.enqueue(('alice','room123',mid,'request '+str(mid),0))
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'requires_consent':True,'message':'welcome'})):
            b.run_one();b.run_one()
        with b.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='awaiting_onboarding'").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notifications WHERE choice IS NOT NULL').fetchone()[0],1)

    def test_restart_recovers_only_never_run_requests_for_completed_owner(self):
        b=self.choice_bridge()
        alice,_=b.enqueue(('alice','room123',10,'first request',0))
        interrupted,_=b.enqueue(('alice','room123',11,'already attempted',0))
        with b.db() as db:
            db.execute("UPDATE jobs SET status='awaiting_onboarding' WHERE id=?",(alice,))
            db.execute("UPDATE jobs SET status='interrupted' WHERE id=?",(interrupted,))
            db.execute("INSERT INTO jobs (id,uid,room,message_id,kind,prompt,status,created,thread) VALUES ('foreign','bob','room123',30,'task','private','awaiting_onboarding',0,0)")
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'projects':[{'room':'room123'}],'onboarding_complete':False})):
            b.refresh('alice')
        with b.db() as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(alice,)).fetchone()[0],'awaiting_onboarding')
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'projects':[{'room':'room123'}],'onboarding_complete':True})):
            b.refresh('alice');b.refresh('alice')
        with b.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(alice,)).fetchone()[0],'queued')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(interrupted,)).fetchone()[0],'interrupted')
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id='foreign'").fetchone()[0],'awaiting_onboarding')

    def test_onboarding_wait_budget_keeps_room_for_explicit_choice(self):
        b=self.choice_bridge()
        for mid in range(1,17):
            ident,_=b.enqueue(('alice','room123',mid,'request',0))
            with b.db() as db:db.execute("UPDATE jobs SET status='awaiting_onboarding' WHERE id=?",(ident,))
        with self.assertRaises(Denied):b.enqueue(('alice','room123',17,'more work',0))
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request',return_value=(200,{'revoked':True})):
            self.assertTrue(b.enqueue(('alice','room123',18,'暂不授权',0))[1])

    def test_completed_setup_persists_without_changing_existing_permission(self):
        with mock.patch.object(self.c.nc,'room'):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
        restarted=Connector(self.config,self.root/'control',self.root/'workspace')
        self.assertIs(restarted.consent(),False)

if __name__=='__main__':unittest.main()
