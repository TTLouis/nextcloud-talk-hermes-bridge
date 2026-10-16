"""Content-free provider response diagnostics; never persist prompts or output text."""
import json

RECOVERY_HINT=('上一次模型响应没有可用正文或函数调用。请接续当前用户已授权的请求，返回实际答复或调用已提供的工具；'
               '生图须通过 assistant 的 generate_image，再用 present_file 交付。若工具尚未加载，先用 tool_describe 查询。'
               '已有成功工具结果不要重复执行。本提示不授予任何新权限；缺少用户选择时调用 ask_user。')

def empty_completion(raw):
    """Only a confirmed empty stop is recoverable, never reasoning/tool/refusal/error."""
    frames=[]
    try:frames=[json.loads(raw)]
    except (ValueError,UnicodeError):
        for line in raw.splitlines():
            if line.startswith(b'data:') and line[5:].strip()!=b'[DONE]':
                try:frames.append(json.loads(line[5:]))
                except (ValueError,UnicodeError):return False
    stopped=False
    for frame in frames:
        if not isinstance(frame,dict) or frame.get('error'):return False
        choices=frame.get('choices',[])
        if not isinstance(choices,list):return False
        for c in choices:
            if not isinstance(c,dict):return False
            reason=c.get('finish_reason')
            if reason not in (None,'stop'):return False
            stopped=stopped or reason=='stop'
            msg=c.get('message') or c.get('delta') or {}
            if not isinstance(msg,dict):return False
            if any(msg.get(k) for k in ('tool_calls','function_call','refusal','reasoning','reasoning_content','reasoning_details')):return False
            content=msg.get('content')
            if content is not None and (not isinstance(content,str) or content.strip()):return False
    return stopped

def recovery_request(data):
    # Preserve the exact model, tools, history and attachments. The hint changes
    # neither native input provenance nor Connector grants/approval decisions.
    request={k:v for k,v in data.items() if k!='stream_options'}
    return {**request,'stream':False,'messages':[*data['messages'],{'role':'user','content':RECOVERY_HINT}]}

def as_stream(raw):
    """OpenAI SDK asked for SSE; wrap a non-streaming retry without losing tools."""
    response=json.loads(raw)
    choices=[]
    for i,c in enumerate(response['choices']):
        msg=dict(c['message'])
        if msg.get('tool_calls'):
            msg['tool_calls']=[{**t,'index':n} for n,t in enumerate(msg['tool_calls'])]
        choices.append({'index':c.get('index',i),'delta':msg,'finish_reason':c.get('finish_reason')})
    frame={k:response[k] for k in ('id','created','model','usage','system_fingerprint') if k in response}
    frame.update({'object':'chat.completion.chunk','choices':choices})
    return b'data: '+json.dumps(frame).encode()+b'\n\ndata: [DONE]\n\n'

def response_shape(raw, request):
    frames=[]
    try:
        frames=[json.loads(raw)]
    except (ValueError,UnicodeError):
        for line in raw.splitlines():
            if line.startswith(b'data:') and line[5:].strip()!=b'[DONE]':
                try:frames.append(json.loads(line[5:]))
                except (ValueError,UnicodeError):pass
    tools=request.get('tools');tools=tools if isinstance(tools,list) else []
    result={'frames':len(frames),'stream':bool(request.get('stream')),'visible_chars':0,
            'reasoning_chars':0,'function_calls':0,'finish_reasons':[], 'output_types':[],
            'image_tool_visible':any(isinstance(t,dict) and isinstance(t.get('function'),dict) and t['function'].get('name')=='mcp__assistant__generate_image' for t in tools)}
    calls_seen=set()
    reasons={'stop','length','tool_calls','content_filter','function_call'}
    types={'message','function_call','reasoning','image_generation_call'}
    for frame in frames:
        if not isinstance(frame,dict):continue
        usage=frame.get('usage')
        if isinstance(usage,dict):
            result['tokens']={k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens') and type(v) is int and 0<=v<10000000}
        choices=frame.get('choices');choices=choices if isinstance(choices,list) else []
        for i,choice in enumerate(choices):
            if not isinstance(choice,dict):continue
            reason=choice.get('finish_reason')
            if reason is not None:
                reason=reason if isinstance(reason,str) and reason in reasons else 'other'
                if reason not in result['finish_reasons']:result['finish_reasons'].append(reason)
            msg=choice.get('message') or choice.get('delta') or {}
            if not isinstance(msg,dict):continue
            for field,key in [('content','visible_chars'),('reasoning_content','reasoning_chars'),('reasoning','reasoning_chars')]:
                if isinstance(msg.get(field),str):result[key]+=len(msg[field])
            calls=msg.get('tool_calls') or []
            if isinstance(calls,list):
                for n,call in enumerate(calls):
                    if isinstance(call,dict):
                        index=call.get('index',n);index=index if type(index) is int else n
                        calls_seen.add((i,index))
        output=frame.get('output');output=output if isinstance(output,list) else []
        for item in output:
            if isinstance(item,dict):
                kind=item.get('type');kind=kind if isinstance(kind,str) and kind in types else 'other'
                if kind not in result['output_types']:result['output_types'].append(kind)
    result['function_calls']=len(calls_seen)
    return result
