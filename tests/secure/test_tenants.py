import json
from pathlib import Path
import sys
import tempfile
import unittest
DEPLOY = Path('/deploy') if Path('/deploy').exists() else Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY))
from tenant_config import compose_for_tenants, tenant_configs


class TenantDeploymentTests(unittest.TestCase):
    def test_public_browser_network_is_only_added_to_registered_connectors(self):
        template=json.loads((DEPLOY.parent/'compose.personal.yaml').read_text())
        data=compose_for_tenants(template,{'ttauth':{'browser_url':'http://hermes-talk-browser:9120'},'mmfelix':{}})
        self.assertIn('web-calls',data['services']['runner-ttauth']['networks'])
        self.assertNotIn('web-calls',data['services']['runner-mmfelix']['networks'])
        self.assertEqual(data['services']['worker-ttauth'],template['services']['worker-ttauth'])
        self.assertEqual(data['services']['worker-mmfelix']['networks'],['worker-mmfelix'])
        self.assertEqual(data['networks']['web-calls'],{'external':True,'name':'hermes-talk-web-calls'})

    def test_two_users_keep_separate_worker_networks_mounts_and_global_bridge(self):
        template = json.loads((DEPLOY.parent / 'compose.personal.yaml').read_text())
        data = compose_for_tenants(template, ['ttauth', 'mmfelix'])
        self.assertEqual(len(data['services']), 6)
        self.assertEqual(data['services']['runner-ttauth'], template['services']['runner-ttauth'])
        self.assertEqual(data['services']['worker-ttauth'], template['services']['worker-ttauth'])
        self.assertEqual(data['services']['worker-mmfelix']['networks'], ['worker-mmfelix'])
        self.assertEqual(data['services']['worker-mmfelix']['volumes'], ['./secrets/worker-mmfelix.json:/run/config/worker.json:ro', './workspace-mmfelix:/workspace'])
        self.assertNotIn('worker-ttauth', data['services']['runner-mmfelix']['networks'])
        self.assertEqual(set(data['services']['bridge']['depends_on']), {'runner-ttauth', 'runner-mmfelix'})
        self.assertEqual(len([s for s in data['services'] if s == 'bridge']), 1)
        self.assertEqual(template['services']['bridge']['depends_on'], {'runner-ttauth': {'condition': 'service_healthy'}})

    def fixture(self):
        tmp = tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root = Path(tmp.name);secret = root / 'secrets';secret.mkdir()
        bridge = {'bindings': []};delivery = {'tenants': []}
        for slug in ['alice', 'bob']:
            c = {'uid': slug, 'room': slug+'123', 'rooms': [slug+'123'], 'runner_token': slug+'dispatch', 'worker_token': slug+'worker', 'delivery_token': slug+'delivery', 'worker_url': 'http://hermes-talk-worker-'+slug+':8790'}
            w = {'uid': slug, 'runner_token': c['worker_token'], 'connector_url': 'http://hermes-talk-runner-'+slug+':8789'}
            (secret/('connector-'+slug+'.json')).write_text(json.dumps(c));(secret/('worker-'+slug+'.json')).write_text(json.dumps(w))
            bridge['bindings'].append({'uid': slug, 'room': c['room'], 'runner_token': c['runner_token'], 'runner_url': w['connector_url']})
            delivery['tenants'].append({'uid': slug, 'rooms': c['rooms'], 'token': c['delivery_token']})
        (secret/'bridge-v2.json').write_text(json.dumps(bridge));(secret/'delivery.json').write_text(json.dumps(delivery))
        return root

    def test_existing_tenants_are_loaded_without_modification(self):
        root = self.fixture();before = {p.name: p.read_bytes() for p in (root/'secrets').iterdir()}
        self.assertEqual(set(tenant_configs(root)), {'alice', 'bob'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in (root/'secrets').iterdir()})

    def test_shared_token_or_swapped_worker_fails_closed(self):
        root = self.fixture();path = root/'secrets/connector-bob.json';original = json.loads(path.read_text())
        shared = {**original, 'runner_token': 'alicedispatch'};path.write_text(json.dumps(shared))
        bridge_path = root/'secrets/bridge-v2.json';b = json.loads(bridge_path.read_text());b['bindings'][1]['runner_token'] = shared['runner_token'];bridge_path.write_text(json.dumps(b))
        with self.assertRaises(ValueError):tenant_configs(root)
        b['bindings'][1]['runner_token'] = original['runner_token'];bridge_path.write_text(json.dumps(b))
        path.write_text(json.dumps({**original, 'worker_url': 'http://hermes-talk-worker-alice:8790'}))
        with self.assertRaises(AssertionError):tenant_configs(root)

    def test_unsafe_or_duplicate_compose_tenant_is_rejected(self):
        template = json.loads((DEPLOY.parent / 'compose.personal.yaml').read_text())
        for slugs in [[], ['alice', 'alice'], ['../alice'], ['alice:/state']]:
            with self.assertRaises(ValueError):compose_for_tenants(template, slugs)


if __name__ == '__main__':unittest.main()
