import json
import unittest
from nextcloud_talk_hermes_bridge.assistant_response import response_shape,empty_completion,recovery_request,as_stream

class ResponseContracts(unittest.TestCase):
    def shape(self,message,finish='stop'):
        return response_shape(json.dumps({'choices':[{'message':message,'finish_reason':finish}]}).encode(),{})

    def test_empty_reasoning_and_tool_responses_are_distinct(self):
        empty=self.shape({'content':None})
        reasoning=self.shape({'content':None,'reasoning_content':'private reasoning'})
        tool=self.shape({'content':None,'tool_calls':[{'type':'function','function':{'name':'generate_image','arguments':'private prompt'}}]},'tool_calls')
        self.assertEqual(empty['visible_chars'],0);self.assertEqual(empty['function_calls'],0)
        self.assertGreater(reasoning['reasoning_chars'],0);self.assertEqual(tool['function_calls'],1)
        self.assertEqual(tool['finish_reasons'],['tool_calls'])
        self.assertNotIn('private',json.dumps([empty,reasoning,tool]))

    def test_sse_chunks_and_usage_do_not_persist_content_or_arbitrary_fields(self):
        frames=[{'choices':[{'delta':{'content':'secret text'}}]},
                {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':12,'completion_tokens':3,'secret':'password'}}]
        raw=b'\n\n'.join(b'data: '+json.dumps(f).encode() for f in frames)+b'\n\ndata: [DONE]\n'
        out=response_shape(raw,{'stream':True,'tools':[{'function':{'name':'mcp__assistant__generate_image'}}]})
        self.assertEqual(out['visible_chars'],11);self.assertEqual(out['tokens'],{'prompt_tokens':12,'completion_tokens':3})
        self.assertTrue(out['image_tool_visible']);self.assertNotIn('secret',json.dumps(out))

    def test_unrecognized_provider_fields_never_become_log_text(self):
        raw=json.dumps({'choices':[{'finish_reason':'SECRET_TOKEN','message':{'content':None}}],'output':[{'type':'PRIVATE_URL'}]}).encode()
        out=response_shape(raw,{})
        self.assertEqual(out['finish_reasons'],['other']);self.assertEqual(out['output_types'],['other'])
        self.assertNotIn('SECRET',json.dumps(out));self.assertNotIn('PRIVATE',json.dumps(out))
        self.assertEqual(response_shape(b'not json',{})['frames'],0)

    def test_only_confirmed_empty_stop_can_trigger_recovery(self):
        raw=lambda m,reason='stop':json.dumps({'choices':[{'message':m,'finish_reason':reason}]}).encode()
        self.assertTrue(empty_completion(raw({'content':None})))
        self.assertTrue(empty_completion(as_stream(raw({'content':'  '}))))
        for m in [{'content':'answer'},{'reasoning_content':'thinking'},
                  {'refusal':'no'},{'tool_calls':[{'function':{'name':'tool'}}]}, {'content':[{'type':'image_url'}]}]:
            self.assertFalse(empty_completion(raw(m)))
        for reason in ['length','content_filter','tool_calls',None]:self.assertFalse(empty_completion(raw({'content':None},reason)))
        for v in [b'{}',b'not json',b'data: [DONE]\n',b'data: {broken}\n']:self.assertFalse(empty_completion(v))

    def test_recovery_preserves_model_tools_history_images_and_does_not_mutate_input(self):
        data={'model':'gpt-6.1-sol','stream':True,'stream_options':{'include_usage':True},'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:private'}}]}],
              'tools':[{'function':{'name':'original_tool'}}],'metadata':{'session_id':'owner-session'}}
        before=json.dumps(data);out=recovery_request(data)
        self.assertEqual(out['messages'][:-1],data['messages']);self.assertFalse(out['stream'])
        for k in ['model','tools','metadata']:self.assertEqual(out[k],data[k])
        self.assertEqual(json.dumps(data),before)
        self.assertNotIn('stream_options',out)

    def test_nonstream_recovery_is_valid_sse_with_intact_tool_id_and_arguments(self):
        obj={'id':'chatcmpl-test','model':'gpt-6.1-sol','choices':[{'index':0,'message':{'role':'assistant','content':None,
             'tool_calls':[{'id':'call-1','type':'function','function':{'name':'mcp__assistant__generate_image','arguments':'{"prompt":"lake"}'}}]},'finish_reason':'tool_calls'}]}
        stream=as_stream(json.dumps(obj).encode());frame=json.loads(stream.splitlines()[0][6:])
        tool=frame['choices'][0]['delta']['tool_calls'][0]
        self.assertEqual(tool['index'],0);self.assertEqual(tool['id'],'call-1');self.assertEqual(tool['function']['arguments'],'{"prompt":"lake"}')
        self.assertFalse(empty_completion(stream));self.assertEqual(response_shape(stream,{})['function_calls'],1)

if __name__=='__main__':unittest.main()
