"""Local canonical-token replay; never imports the live collector or API client."""
import copy
import json
from array import array
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer


class RadixCache:
    """Unbounded prefix cache with compressed token edges, independent per trajectory."""
    def __init__(self):self.root={}

    def match(self,tokens):
        node=self.root;i=0
        while i<len(tokens) and tokens[i] in node:
            edge,child=node[tokens[i]];j=0
            while j<len(edge) and i+j<len(tokens) and edge[j]==tokens[i+j]:j+=1
            i+=j
            if j<len(edge):break
            node=child
        return i

    def insert(self,tokens):
        node=self.root;i=0
        while i<len(tokens):
            key=tokens[i]
            if key not in node:
                node[key]=(array('I',tokens[i:]),{});return
            edge,child=node[key];j=0
            while j<len(edge) and i+j<len(tokens) and edge[j]==tokens[i+j]:j+=1
            if j<len(edge):
                branch={edge[j]:(edge[j:],child)}
                node[key]=(edge[:j],branch)
                i+=j
                if i<len(tokens):branch[tokens[i]]=(array('I',tokens[i:]),{})
                return
            i+=j;node=child


def normalize(messages):
    result=copy.deepcopy(messages)
    for message in result:
        if message.get('role') not in ('system','user','assistant','tool'):
            raise ValueError('Unsupported role; cannot silently omit a message')
        for tool in message.get('tool_calls') or []:
            function=tool.get('function',tool)
            if isinstance(function.get('arguments'),str):
                function['arguments']=json.loads(function['arguments'])
            if not isinstance(function.get('arguments'),dict):
                raise ValueError('Tool arguments must reconstruct to an object')
    return result


class Renderer:
    def __init__(self,assets):
        self.tokenizer=Tokenizer.from_file(str(assets/'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        environment=ImmutableSandboxedEnvironment(trim_blocks=True,lstrip_blocks=True,
            extensions=['jinja2.ext.loopcontrols'])
        environment.filters['tojson']=lambda value,**kwargs:json.dumps(value,**kwargs)
        self.template=environment.from_string((assets/'chat_template.jinja').read_text(encoding='utf-8'))

    def encode(self,body,messages=None,generation=True):
        text=self.template.render(messages=normalize(body['messages'] if messages is None else messages),
            tools=body.get('tools') or [],add_generation_prompt=generation,
            reasoning_effort=body.get('reasoning_effort','max'),clear_thinking=False)
        return self.tokenizer.encode(text,add_special_tokens=False).ids


def hits(length,prefix):
    return {size:(prefix//size)*size for size in (1,16,64)}


def output_issue(choice,usage):
    message=choice.get('message')
    if not message:return 'no_message'
    if choice.get('finish_reason') not in ('stop','tool_calls'):
        return 'output_termination_not_reconstructable'
    reasoning=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
    if reasoning!=0 and not message.get('reasoning_content') and '<think>' not in (message.get('content') or ''):
        return 'hidden_reasoning_unavailable'
    return None
