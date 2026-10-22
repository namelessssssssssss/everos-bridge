"""Opt-in real Claude Code / MiMo client dispatch with fixed local model and memory APIs.

No real model calls, credentials, or user configuration are needed. This validates native tool dispatch,
not model quality. Complements validate_trae.py and the configured-entrypoint audit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from audit_harnesses import Fixture

ROOT = Path(__file__).resolve().parents[1]


class Gateway(Fixture):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        if self.path.endswith(('/memory/search','/memory/get')):
            return super().do_POST()
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(data)
        messages = data.get('messages') or []
        tools = [(t.get('function') or t).get('name','') for t in data.get('tools') or []]
        done = [call.get('function',{}).get('name','') for m in messages for call in m.get('tool_calls',[])]
        done += [b.get('name','') for m in messages for b in (m.get('content') or [])
                 if isinstance(m.get('content'),list) and isinstance(b,dict) and b.get('type')=='tool_use']
        search = next((n for n in tools if n.endswith('memory_search')),None)
        opened = next((n for n in tools if n.endswith('memory_open')),None)
        name, args = None, None
        if search and search not in done:
            name,args=search,{'query':'harness fixture','method':'agentic'}
        elif opened and opened not in done:
            ref=re.search(r'em_[a-f0-9]{40}',json.dumps(messages))
            if ref: name,args=opened,{'memory_id':ref.group(0)}
        result='HARNESS_EXPANSION_OK' if 'HARNESS_EXPANSION_OK' in json.dumps(messages) else 'INCOMPLETE_FLOW'
        if name:self.server.tool_calls.append(name)
        anthropic=self.path.split('?',1)[0].endswith('/messages')
        streaming=bool(data.get('stream',False))
        self.send_response(200);self.send_header('Content-Type','text/event-stream' if streaming else 'application/json');self.end_headers()
        if anthropic:
            msg={'id':'msg-fixture','type':'message','role':'assistant','model':data.get('model','fixture'),
                 'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}}
            block={'type':'tool_use','id':'call-'+str(len(done)),'name':name,'input':args} if name else {'type':'text','text':result}
            stop='tool_use' if name else 'end_turn'
            if streaming:
                events=[('message_start',{'type':'message_start','message':msg}),
                        ('content_block_start',{'type':'content_block_start','index':0,'content_block':dict(block,**({'input':{}} if name else {'text':''}))}),
                        ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(args)} if name else {'type':'text_delta','text':result}}),
                        ('content_block_stop',{'type':'content_block_stop','index':0}),
                        ('message_delta',{'type':'message_delta','delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':1}}),
                        ('message_stop',{'type':'message_stop'})]
                for event,body in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(body)+'\n\n').encode())
            else:
                msg.update(content=[block],stop_reason=stop);self.wfile.write(json.dumps(msg).encode())
        else:
            message={'role':'assistant','content':None if name else result}
            if name:message['tool_calls']=[{'id':'call-'+str(len(done)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]
            finish='tool_calls' if name else 'stop'
            if streaming:
                delta=dict(message)
                if name:delta['tool_calls']=[dict(message['tool_calls'][0],index=0)]
                for d,f in [(delta,finish),({},finish)]:
                    chunk={'id':'fixture','object':'chat.completion.chunk','created':int(time.time()),'model':data.get('model','fixture'),
                           'choices':[{'index':0,'delta':d,'finish_reason':f}]}
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.wfile.write(json.dumps({'id':'fixture','object':'chat.completion','created':int(time.time()),'model':data.get('model','fixture'),
                                             'choices':[{'index':0,'message':message,'finish_reason':finish}],
                                             'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode())
        self.wfile.flush()


def validate(host, root):
    root=root.resolve();root.mkdir(parents=True,exist_ok=False)
    server=ThreadingHTTPServer(('127.0.0.1',0),Gateway);server.requests=[];server.tool_calls=[]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    env={k:v for k,v in os.environ.items() if not k.startswith(('EVEROS_','ANTHROPIC_','CLAUDE_','MIMOCODE_'))}
    env.update(EVEROS_HOME=str(root/'memory'),EVEROS_USER_ID='fixture-owner',EVEROS_API_KEY='fixture-key',
               EVEROS_API_VERSION='v2',EVEROS_READ_V1='0',EVEROS_WRITE='0',EVEROS_API_BASE=base)
    mcp={'command':sys.executable,'args':[str(ROOT/'plugin/everos/mcp_server.py')], 'env':{k:v for k,v in env.items() if k.startswith('EVEROS_')}}
    prompt='Read the harness fixture with memory_search and then expand its em_ ID with memory_open. Return the marker in the expanded memory. Use only these read-only tools.'
    if host=='claude':
        config=root/'mcp.json';config.write_text(json.dumps({'mcpServers':{'everos':mcp}}))
        env.update(CLAUDE_CONFIG_DIR=str(root/'claude-home'),ANTHROPIC_API_KEY='fixture-key',ANTHROPIC_BASE_URL=base,ANTHROPIC_MODEL='orange-5.5',EVEROS_INJECT='0')
        command=['claude','-p',prompt,'--mcp-config',str(config),'--strict-mcp-config','--settings','{"disableAllHooks":true}',
                 '--allowedTools','mcp__everos__memory_search','mcp__everos__memory_open','--output-format','stream-json','--verbose','--no-session-persistence']
    else:
        cfg=root/'config/mimocode';cfg.mkdir(parents=True)
        modules=Path.home()/'.config/mimocode/node_modules'
        if modules.exists():(cfg/'node_modules').symlink_to(modules,target_is_directory=True)
        data={'model':'fixture/fixture','provider':{'fixture':{'npm':'@ai-sdk/openai-compatible','name':'Fixture',
               'options':{'baseURL':base+'/v1','apiKey':'fixture-key'},'models':{'fixture':{'name':'Fixture','limit':{'context':128000,'output':1024}}}}},
              'mcp':{'everos':{'type':'local','command':[mcp['command']]+mcp['args'], 'environment':mcp['env']}},
              'permission':{'*':'deny','everos_*':'allow'},
              'plugin':[[ (ROOT/'plugin/mimo/everos.mjs').as_uri(),{'hook':str(ROOT/'plugin/everos/hook.py')}]]}
        (cfg/'mimocode.jsonc').write_text(json.dumps(data))
        env.update(HOME=str(root/'fixture-home'),XDG_CONFIG_HOME=str(root/'config'),XDG_DATA_HOME=str(root/'data'),XDG_CACHE_HOME=str(root/'cache'),EVEROS_INJECT='1')
        command=['mimo','run','--format','json','--dir',str(root),prompt]
    try:
        result=subprocess.run(command,cwd=root,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=90)
        (root/'stdout.jsonl').write_text(result.stdout);(root/'stderr.txt').write_text(result.stderr)
        (root/'model-requests.json').write_text(json.dumps(server.requests,ensure_ascii=False))
        report={'host':host,'exit':result.returncode,'native_tool_calls':server.tool_calls,
                'expanded_memory_in_model':any('HARNESS_EXPANSION_OK' in json.dumps(d) for d in server.requests),
                'fixed_local_model':True,'real_credentials_used':False}
        report['passed']=result.returncode==0 and any(n.endswith('memory_open') for n in server.tool_calls) and report['expanded_memory_in_model']
        (root/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
        return report['passed']
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',choices=['claude','mimo'],required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();sys.exit(0 if validate(args.host,args.out) else 1)
