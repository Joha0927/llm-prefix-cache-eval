import json
from core import RadixCache,hits,normalize,output_issue


def test_branch_reuses_any_past_prefix_without_future_leak():
    cache=RadixCache()
    assert cache.match([1,2,3])==0
    cache.insert([1,2,3]);cache.insert([1,2,4,5])
    assert cache.match([1,2,3,6])==3
    assert cache.match([1,2,4,6])==3
    assert cache.match([1,2])==2
    cache.insert([1,2])
    assert cache.match([1,2,3])==3
    assert cache.match([9,1,2])==0


def test_blocks_require_a_complete_matching_prefix():
    assert hits(100,63)=={1:63,16:48,64:0}
    assert hits(100,64)=={1:64,16:64,64:64}


def test_outputs_become_available_only_after_response():
    cache=RadixCache();prompt=[1,2];generated=[1,2,3,4]
    assert cache.match(prompt)==0
    cache.insert(prompt);cache.insert(generated)
    assert cache.match([1,2,3,4,5])==4
    assert RadixCache().match(generated)==0


def test_tool_argument_conversion_preserves_order_and_input():
    messages=[{'role':'assistant','tool_calls':[{'function':{'name':'bash','arguments':'{"z":1,"a":2}'}}]}]
    parsed=normalize(messages)
    assert list(parsed[0]['tool_calls'][0]['function']['arguments'])==['z','a']
    assert isinstance(messages[0]['tool_calls'][0]['function']['arguments'],str)


def test_invalid_tool_args_fail_instead_of_silent_loss():
    try:normalize([{'role':'assistant','tool_calls':[{'function':{'arguments':'broken'}}]}])
    except json.JSONDecodeError:return
    raise AssertionError('Expected rejected reconstruction')


def test_hidden_thinking_is_never_invented():
    choice={'finish_reason':'stop','message':{'role':'assistant','content':'visible'}}
    assert output_issue(choice,{'completion_tokens_details':{'reasoning_tokens':12}})=='hidden_reasoning_unavailable'
    assert output_issue(choice,{'completion_tokens_details':{'reasoning_tokens':0}}) is None
    assert output_issue(choice,{})=='hidden_reasoning_unavailable'


def test_truncated_output_is_not_given_a_synthetic_terminator():
    choice={'finish_reason':'length','message':{'reasoning_content':'unfinished thought','content':''}}
    assert output_issue(choice,{})=='output_termination_not_reconstructable'
