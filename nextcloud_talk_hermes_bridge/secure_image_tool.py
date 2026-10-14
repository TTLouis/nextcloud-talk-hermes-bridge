"""Task-bound MCP image tool; one billable image request per immutable task ID."""
import base64
import json
import os
from pathlib import Path
import re
import sys
from .secure_common import MAX_IMAGE_BYTES, json_request

def generate(prompt):
    config=json.loads(Path(os.environ['IMAGE_TOOL_CONFIG']).read_text())
    job=os.environ['IMAGE_JOB_ID']
    if not re.fullmatch('[a-f0-9]{12}',job) or not isinstance(prompt,str) or not 1<=len(prompt)<=8000:
        raise ValueError('Invalid task or prompt')
    directory=Path(os.environ['IMAGE_TASK_ROOT'])/job
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/'image.png'
    if target.exists():
        return {'success':True,'artifact':job,'already_generated':True}
    claim=directory/'generation.claimed'
    # Exclusive creation makes repeated calls/restarts fail closed instead of billing twice.
    with claim.open('x') as handle:
        handle.write(job)
    _,result=json_request(config['base_url']+'/images/generations',method='POST',
         headers={'Authorization':'Bearer '+config['api_key']},timeout=240,limit=40*1024*1024,
         body={'model':config['image_model'],'prompt':prompt,'n':1,'size':'1024x1024'})
    binary=base64.b64decode(result['data'][0]['b64_json'],validate=True)
    if not binary.startswith(b'\x89PNG\r\n\x1a\n') or len(binary)>MAX_IMAGE_BYTES:
        raise ValueError('Unsupported or oversized image output')
    temporary=directory/'image.partial'
    temporary.write_bytes(binary)
    temporary.replace(target)
    return {'success':True,'artifact':job}

def main():
    # A deliberately small stdio MCP server; avoids SDK server API/version coupling.
    for line in sys.stdin:
        if len(line)>65536:
            return
        message=json.loads(line)
        if 'id' not in message:
            continue
        method=message.get('method')
        response={'jsonrpc':'2.0','id':message['id']}
        if method=='initialize':
            response['result']={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},
                                'serverInfo':{'name':'task_image','version':'1.0.0'}}
        elif method=='ping':
            response['result']={}
        elif method=='tools/list':
            response['result']={'tools':[{'name':'generate_image',
                'description':'Generate exactly one requested image. The host returns the attachment and asks about saving.',
                'inputSchema':{'type':'object','properties':{'prompt':{'type':'string','minLength':1,'maxLength':8000}},
                               'required':['prompt'],'additionalProperties':False}}]}
        elif method=='tools/call':
            try:
                params=message.get('params',{})
                if params.get('name')!='generate_image':
                    raise ValueError('Unknown tool')
                result=generate(params['arguments']['prompt'])
                response['result']={'content':[{'type':'text','text':json.dumps(result)}]}
            except Exception as exc:
                response['result']={'isError':True,'content':[{'type':'text','text':'Image generation failed: '+type(exc).__name__}]}
        else:
            response['error']={'code':-32601,'message':'Method not found'}
        print(json.dumps(response),flush=True)

if __name__=='__main__':
    main()
