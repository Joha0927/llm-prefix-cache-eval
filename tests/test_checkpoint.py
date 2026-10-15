import hashlib
import json
import time
from types import SimpleNamespace
import pytest
from minisweagent.exceptions import Submitted
from glm_trace.checkpoint import continue_agent, load_checkpoint


def test_resume_keeps_history_counters_and_active_time():
    state={'resume_allowed':True,'messages':[{'role':'user','content':'original context'}],
        'n_calls':161,'cost':2.5,'n_consecutive_format_errors':0,'active_seconds':90}
    class Agent:
        config=SimpleNamespace(output_path=None,max_consecutive_format_errors=3)
        extra_template_vars={}
        def step(self):
            assert self.messages==state['messages']
            assert self.n_calls==161
            self.n_calls+=1
            raise Submitted({'role':'exit','content':'done','extra':{'exit_status':'Submitted'}})
        def add_messages(self,*messages):self.messages.extend(messages)
        def save(self,path):pass
    agent=Agent()
    result=continue_agent(agent,state,'original task')
    assert result['exit_status']=='Submitted' and agent.n_calls==162
    assert agent.cost==2.5 and time.time()-agent._start_time>=90
    assert agent.extra_template_vars['task']=='original task'


def test_corrupt_or_wrong_task_checkpoint_is_rejected(tmp_path):
    state={'resume_allowed':True,'ids':{'task_id':'task'},'messages':[]}
    path=tmp_path/'checkpoint.json';path.write_text(json.dumps(state))
    (tmp_path/'checkpoint_manifest.json').write_text(json.dumps({'hashes':{
        'checkpoint.json':hashlib.sha256(path.read_bytes()).hexdigest()}}))
    assert load_checkpoint(tmp_path,'task')[0]['ids']['task_id']=='task'
    with pytest.raises(RuntimeError,match='Wrong task'):load_checkpoint(tmp_path,'other')
    path.write_text(json.dumps({**state,'messages':[{'content':'changed'}]}))
    with pytest.raises(RuntimeError,match='hash mismatch'):load_checkpoint(tmp_path,'task')
