import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
from nextcloud_talk_hermes_bridge.assistant_connector import Connector
from nextcloud_talk_hermes_bridge.assistant_bridge import Bridge, Denied
from nextcloud_talk_hermes_bridge.assistant_context import task_workspace, write_workspace
from nextcloud_talk_hermes_bridge.assistant_outputs import readable_filename, valid_filename, readable_component
from nextcloud_talk_hermes_bridge.assistant_replies import mobile_reply, worker_reply, DETAILS_MARKER


class MobileOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'uid': 'alice', 'room': 'room123', 'rooms': ['room123'], 'runner_token': 'dispatch',
            'worker_url': 'http://worker', 'worker_token': 'worker', 'nextcloud_url': 'https://cloud.invalid',
            'nextcloud_token': 'secret', 'assistant_uid': 'hermes-assistant', 'model_url': 'http://model/v1',
            'model_token': 'model-secret', 'chat_model': 'gpt-6.1-sol', 'image_model': 'gpt-image-2.5',
            'delivery_url': 'http://delivery', 'delivery_token': 'delivery'}
        self.c = Connector(self.cfg, self.root/'control', self.root/'workspace')
        self.p = {'uid': 'alice', 'room': 'room123', 'thread': 12, 'job_id': 'a'*12,
                  'kind': 'task', 'prompt': 'test', 'task_name': '家庭预算'}
        self.raw = '预算说明\n\n- 金额：300 元\n- 期限：周五\n- 风险：支出尚未确认\n\n' + DETAILS_MARKER + '\n\n' + ('完整条目，不省略。\n'*1700) + '末尾重要信息\n'

    def retain(self, raw=None, p=None):
        p = p or self.p
        work = task_workspace(self.c.workspace, p['uid'], p['room'], p['thread'])
        result = worker_reply(self.c.workspace, work, p['job_id'], self.raw if raw is None else raw)
        self.c.retain_reply(p, work, result)
        return result

    def test_short_reply_preserves_paragraphs_and_bullets_without_attachment(self):
        text = '结果已确认。\n\n- 金额：300 元\n- 周五完成。'
        self.assertEqual(mobile_reply(text), (text, False))

    def test_full_original_over_12000_characters_is_retained_verbatim(self):
        self.assertGreater(len(self.raw), 12000)
        result = self.retain()
        ident = result['details']['artifact']
        self.assertEqual((self.c.state/'artifacts'/ident).read_bytes(), self.raw.encode())
        self.assertIn('- 金额：300 元', result['message'])
        self.assertNotIn(DETAILS_MARKER, result['message'])
        self.assertLess(len(result['message']), 700)
        with self.c.db() as db:
            self.assertEqual(db.execute('SELECT status FROM artifacts').fetchone()[0], 'prepared')

    def test_unstructured_long_reply_is_labelled_excerpt_with_original_retained(self):
        raw = '第一段。\n\n' + '很多详细信息。'*400 + '\n最后信息'
        result = self.retain(raw)
        self.assertIn('节选', result['message'])
        self.assertLessEqual(len(result['message']), 700)
        self.assertEqual((self.c.state/'artifacts'/result['details']['artifact']).read_text(), raw)

    def test_worker_cannot_point_to_another_task_or_fake_snapshot_hash(self):
        work = task_workspace(self.c.workspace, 'alice', 'room123', 12)
        result = worker_reply(self.c.workspace, work, self.p['job_id'], self.raw)
        result['reply_details']['sha256'] = '0'*64
        result['reply_details']['path'] = '../../secret'
        with self.assertRaises(ValueError): self.c.retain_reply(self.p, work, result)
        with self.c.db() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 0)

    def test_details_read_rejects_symlink_in_scratch(self):
        work = task_workspace(self.c.workspace, 'alice', 'room123', 12)
        (work/'scratch'/'reply-details').symlink_to(self.c.state, target_is_directory=True)
        with self.assertRaises(OSError):
            self.c.retain_reply(self.p, work, {'reply_details': {'sha256': '0'*64}})

    def test_details_not_uploaded_until_view_and_not_saved_until_confirmed(self):
        with mock.patch.object(self.c.nc, 'upload_file') as upload, mock.patch.object(self.c, 'delivery_request', return_value=(200, {'share_id': 1})) as delivery:
            result = self.retain(); delivery.assert_not_called(); upload.assert_not_called()
            ident = result['details']['artifact']
            with mock.patch.object(self.c.nc, 'room'), mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request') as worker:
                response = self.c.execute({**self.p, 'kind': 'details', 'artifact': ident})
                worker.assert_not_called()
                self.assertEqual(base64.b64decode(delivery.call_args.kwargs['body']['data']), self.raw.encode())
                self.c.execute({**self.p, 'kind': 'details', 'artifact': ident})
                self.assertEqual(delivery.call_count, 1)
                upload.assert_not_called()
                with mock.patch.object(self.c.nc, 'mkdirs'):
                    self.c.execute({**self.p, 'kind': 'save', 'artifact': ident})
                upload.assert_called_once()
                self.assertEqual(upload.call_args.args[1], self.raw.encode())
                self.assertIn('家庭预算', response['artifacts'][0]['save_path'])

    def test_prepared_details_cannot_be_saved_before_view(self):
        ident = self.retain()['details']['artifact']
        with mock.patch.object(self.c.nc, 'room'), mock.patch.object(self.c.nc, 'upload_file') as upload:
            with self.assertRaises(ValueError): self.c.execute({**self.p, 'kind': 'save', 'artifact': ident})
            upload.assert_not_called()

    def test_details_are_bound_to_owner_room_and_task(self):
        ident = self.retain()['details']['artifact']
        with self.c.db() as db: db.execute('INSERT INTO projects VALUES (?,?)', ('room456', '项目 · 第二项目'))
        with mock.patch.object(self.c.nc, 'room'), mock.patch.object(self.c, 'delivery_request') as delivery:
            for changes in ({'uid': 'bob'}, {'room': 'room456'}, {'thread': 13}):
                with self.assertRaises(PermissionError): self.c.execute({**self.p, **changes, 'kind': 'details', 'artifact': ident})
            delivery.assert_not_called()

    def test_view_does_not_consume_once_model_or_change_consent(self):
        ident = self.retain()['details']['artifact']
        with mock.patch.object(self.c.nc, 'room'), mock.patch.object(self.c, 'delivery_request', return_value=(200, {})), mock.patch.object(self.c, 'assign_task_model') as assign:
            self.assertIsNone(self.c.consent())
            self.c.execute({**self.p, 'kind': 'details', 'artifact': ident})
            assign.assert_not_called(); self.assertIsNone(self.c.consent())

    def test_unicode_name_and_true_extension_are_preserved(self):
        self.assertEqual(readable_filename('out/abcdef012345.png', '湖边夕阳'), '湖边夕阳.png')
        self.assertEqual(readable_filename('out/abcdef012345.png'), '生成图片.png')
        self.assertEqual(readable_filename('家庭预算.txt'), '家庭预算.txt')
        name = readable_filename('x.png', '日'*200)
        self.assertTrue(valid_filename(name)); self.assertTrue(name.endswith('.png'))

    def test_filename_validation_blocks_path_controls_bidi_and_device_names(self):
        for name in ('../a.txt', '/a.txt', 'a\\b.txt', 'a\x00.txt', 'a\u202eb.txt', 'CON.txt', '..', 'name. '):
            self.assertFalse(valid_filename(name), name)
        self.assertTrue(valid_filename(readable_filename('家庭预算（更新）.txt')))
        self.assertNotIn('/', readable_component('../家庭预算'))

    def test_human_folders_are_unique_and_frozen_after_rename(self):
        with self.c.db() as db:
            for room in ('room456', 'room789'): db.execute('INSERT INTO projects VALUES (?,?)', (room, '项目 · 家庭预算'))
        first = self.c.output_folder({**self.p, 'room': 'room456', 'thread': 0})
        second = self.c.output_folder({**self.p, 'room': 'room789', 'thread': 0})
        self.assertEqual(first, 'AI Outputs/家庭预算'); self.assertEqual(second, 'AI Outputs/家庭预算 (2)')
        task = self.c.output_folder({**self.p, 'room': 'room456', 'task_name': '十月支出'})
        self.assertEqual(task, 'AI Outputs/家庭预算/十月支出')
        with self.c.db() as db: db.execute('UPDATE projects SET name=? WHERE room=?', ('项目 · 已改名', 'room456'))
        self.assertEqual(self.c.output_folder({**self.p, 'room': 'room456', 'task_name': '改名'}), task)

    def test_duplicate_filename_gets_number_and_old_paths_remain(self):
        self.c.prepare_snapshot(self.p, 'b'*12, '预算.txt', b'one')
        self.c.prepare_snapshot(self.p, 'c'*12, '预算.txt', b'two')
        with self.c.db() as db:
            rows = list(db.execute('SELECT * FROM artifacts ORDER BY id'))
        self.assertTrue(self.c.artifact_destination(rows[0]).endswith('/预算.txt'))
        self.assertTrue(self.c.artifact_destination(rows[1]).endswith('/预算 (2).txt'))
        with self.c.db() as db:
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)', ('d'*12, 'room123', 12, 'legacy.txt', 'pending', 'hash', None, '{}'))
            legacy = db.execute('SELECT * FROM artifacts WHERE id=?', ('d'*12,)).fetchone()
        self.c.prepare_snapshot(self.p, 'd'*12, 'new-name.txt', b'changed')
        self.assertEqual(self.c.artifact_destination(legacy), 'AI Outputs/room123/' + 'd'*12 + '-legacy.txt')

    def test_native_talk_title_field_drives_agent_context_and_folder(self):
        with mock.patch.object(self.c.nc, 'room'), mock.patch.object(self.c.nc, 'ocs', return_value={'thread': {'id':12,'title':'十月预算'}}):
            self.c.execute({**self.p,'kind':'consent','decision':'deny'})
            with mock.patch('nextcloud_talk_hermes_bridge.assistant_connector.json_request',return_value=(200,{'message':'已收到'})) as worker:
                self.c.execute(self.p)
                self.assertEqual(worker.call_args.kwargs['body']['task_name'],'十月预算')
            self.assertEqual(self.c.output_folder({k:v for k,v in self.p.items() if k!='task_name'}),'AI Outputs/个人助理/十月预算')

    def test_native_task_navigation_uses_talk_title(self):
        with mock.patch.object(self.c.nc,'ocs',return_value=[{'thread':{'id':12,'title':'十月预算'}}]):
            self.assertEqual(self.c.tasks('room123'),[{'id':12,'name':'十月预算'}])

    def test_existing_cloud_file_is_not_overwritten_and_new_path_needs_new_confirmation(self):
        ident = self.retain()['details']['artifact']
        with mock.patch.object(self.c.nc, 'room'), mock.patch.object(self.c, 'delivery_request', return_value=(200, {})), mock.patch.object(self.c.nc, 'mkdirs'):
            self.c.execute({**self.p, 'kind': 'details', 'artifact': ident})
            with mock.patch.object(self.c.nc, 'upload_file', side_effect=urllib.error.HTTPError('url', 412, 'exists', {}, None)) as upload:
                result = self.c.execute({**self.p, 'kind': 'save', 'artifact': ident})
                self.assertEqual(upload.call_count, 1)
                self.assertTrue(result['artifacts'][0]['save_path'].endswith(' (2).md'))
                with self.c.db() as db: self.assertEqual(db.execute('SELECT status FROM artifacts').fetchone()[0], 'pending')

    def test_bridge_presents_one_readable_card_and_replay_does_not_enqueue_twice(self):
        b = Bridge({'assistant_uid':'hermes-assistant','bot_secret':'secret','bindings':[
            {'uid':'alice','room':'room123','runner_url':'http://connector','runner_token':'token'}]}, self.root/'bridge.sqlite')
        b.enqueue(('alice', 'room123', 1, 'hello', 0))
        result = {'message':'已核对预算。\n\n- 金额：300 元\n- 周五完成。', 'details':{'artifact':'b'*12}}
        with mock.patch('nextcloud_talk_hermes_bridge.assistant_bridge.json_request', return_value=(200, result)):
            b.run_one()
        with b.db() as db:
            rows = list(db.execute('SELECT * FROM notifications'))
            self.assertEqual(len(rows), 1); self.assertIn('- 金额', rows[0]['message'])
            choice = json.loads(rows[0]['choice'])
            db.execute('INSERT INTO choices VALUES (?,?,?,?,?,?,?,?,?,?)', ('room123', 20, 'alice', 0, 'room123', 0, json.dumps(choice['options']), choice['group'], 'pending', None))
        event = {'uid':'alice','room':'room123','message_id':20,'emoji':'📄'}
        with self.assertRaises(Denied): b.enqueue_reaction({**event,'uid':'bob'})
        self.assertTrue(b.enqueue_reaction(event)[1]); self.assertFalse(b.enqueue_reaction(event)[1])
        with b.db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='details'").fetchone()[0], 1)


if __name__ == '__main__': unittest.main()
