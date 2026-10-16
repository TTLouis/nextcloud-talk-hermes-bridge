"""Render the existing Docker layout for every registered private tenant."""
import copy
import json
import re


def tenant_configs(root):
    secret = root / 'secrets'
    bridge = json.loads((secret / 'bridge-v2.json').read_text())
    delivery = json.loads((secret / 'delivery.json').read_text())
    result = {}
    identities = set()
    tokens = set()
    for binding in bridge['bindings']:
        match = re.fullmatch(r'http://(?:hermes-talk-)?runner-([a-z][a-z0-9-]{0,31}):8789', binding['runner_url'])
        if not match:
            raise ValueError('Unsupported connector address')
        slug = match[1]
        c = json.loads((secret / ('connector-' + slug + '.json')).read_text())
        w = json.loads((secret / ('worker-' + slug + '.json')).read_text())
        if slug in result or c['uid'] in identities:
            raise ValueError('Duplicate tenant')
        assert binding['uid'] == c['uid'] == w['uid']
        assert binding['room'] == c['room'] and c['room'] in c['rooms']
        assert binding['runner_token'] == c['runner_token']
        assert c['worker_url'] == 'http://hermes-talk-worker-' + slug + ':8790'
        assert w['connector_url'] in ('http://runner-' + slug + ':8789', 'http://hermes-talk-runner-' + slug + ':8789')
        assert c['worker_token'] == w['runner_token']
        tenant = [t for t in delivery['tenants'] if t['uid'] == c['uid']]
        assert len(tenant) == 1 and tenant[0]['token'] == c['delivery_token']
        assert c['room'] in tenant[0]['rooms']
        for token in (c['runner_token'], c['worker_token'], c['delivery_token']):
            if not token or token in tokens:
                raise ValueError('Dispatch tokens must be unique across tenants and roles')
            tokens.add(token)
        if c.get('browser_url') or c.get('browser_token'):
            assert c.get('browser_url')=='http://hermes-talk-browser:9120' and re.fullmatch('[a-f0-9]{64}',c.get('browser_token',''))
            if c['browser_token'] in tokens:raise ValueError('Browser token must be unique per tenant')
            tokens.add(c['browser_token'])
        identities.add(c['uid'])
        result[slug] = c
    assert len(delivery['tenants']) == len(result), 'Unregistered delivery tenant'
    return result


def compose_for_tenants(template, slugs):
    browser_slugs = {slug for slug,c in slugs.items() if isinstance(c,dict) and c.get('browser_url')} if isinstance(slugs,dict) else set()
    slugs = list(slugs)
    if not slugs or len(set(slugs)) != len(slugs):
        raise ValueError('Expected unique tenants')
    data = copy.deepcopy(template)
    connector = data['services'].pop('runner-ttauth')
    worker = data['services'].pop('worker-ttauth')
    data['networks'].pop('worker-ttauth')
    data['services']['bridge']['depends_on'] = {}
    for slug in slugs:
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', slug):
            raise ValueError('Unsafe tenant name')
        for role, original in [('runner', connector), ('worker', worker)]:
            service = copy.deepcopy(original)
            service['container_name'] = 'hermes-talk-' + role + '-' + slug
            service['volumes'] = [v.replace('-ttauth', '-' + slug) for v in service['volumes']]
            service['networks'] = [n.replace('-ttauth', '-' + slug) for n in service['networks']]
            if role == 'runner':
                if slug in browser_slugs:service['networks'].append('web-calls')
                service['depends_on'] = {'worker-' + slug: {'condition': 'service_healthy'}, 'delivery': {'condition': 'service_healthy'}}
                data['services']['bridge']['depends_on']['runner-' + slug] = {'condition': 'service_healthy'}
            data['services'][role + '-' + slug] = service
        data['networks']['worker-' + slug] = {'internal': True}
    if browser_slugs:data['networks']['web-calls']={'external':True,'name':'hermes-talk-web-calls'}
    return data
