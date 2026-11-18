import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_hub.core import Store, run_job, select_agent
from agent_hub.management import save_agent, agent_deleted, job_deleted, endpoint, secret_path
from agent_hub.api_worker import infer

class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'workspace')
        self.config={'name':'自定义模型','transport':'http_api','protocol':'openai','endpoint':'https://example.com/v1','model':'test-model','key':'test-secret-value-unique'}
    def plan(self, aid):
        return {'tasks':[{'id':'write','agent':aid,'prompt':'Reply HUB_OK','validation':{'text_contains':['HUB_OK']}}]}
    def test_secret_private_and_edit_and_discovery(self):
        aid=save_agent(self.store,self.config)
        self.assertNotIn(self.config['key'],json.dumps(self.store.snapshot()))
        self.assertEqual(stat.S_IMODE(secret_path(self.store,aid).stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(secret_path(self.store,aid).parent.stat().st_mode),0o700)
        with self.assertRaises(ValueError): save_agent(self.store,dict(self.config,id=aid,endpoint='https://other.example/v1',key=''))
        save_agent(self.store,dict(self.config,id=aid,name='编辑',key=''))
        self.assertEqual(json.loads(secret_path(self.store,aid).read_text())['key'],self.config['key'])
        agent_deleted(self.store,aid,True); self.store.init({'agents':[]})
        self.assertEqual(self.store.agents(),[])
        self.assertEqual(len(self.store.snapshot()['deleted_agents']),1)
        agent_deleted(self.store,aid,False)
        self.assertEqual(self.store.agents()[0]['name'],'编辑')
        save_agent(self.store,dict(self.config,id=aid,key='',clear_key=True))
        self.assertFalse(self.store.agents()[0]['has_key'])
    def test_builtin_deleted_survives_discovery(self):
        a={'id':'codex','name':'Codex'}
        self.store.init({'agents':[a]}); agent_deleted(self.store,'codex',True)
        self.store.init({'agents':[a]})
        self.assertFalse(self.store.agents()); agent_deleted(self.store,'codex',False)
        self.assertEqual(self.store.agents()[0]['id'],'codex')
    def test_no_auto_api_charge_or_file_tools(self):
        aid=save_agent(self.store,self.config)
        self.assertIsNone(select_agent(self.store,{'kind':'general'}, {})[0])
        self.assertEqual(select_agent(self.store,{'agent':aid}, {})[0]['id'],aid)
        self.assertIsNone(select_agent(self.store,{'agent':aid,'allowed_tools':['files_write']},{})[0])
        save_agent(self.store,dict(self.config,id=aid,auto_route=True))
        self.assertEqual(select_agent(self.store,{}, {})[0]['id'],aid)
    def test_delete_restore_preserves_files_and_prevents_execution(self):
        aid=save_agent(self.store,{'name':'手工','transport':'file_bridge'})
        job=self.store.add_plan(self.plan(aid))
        with self.assertRaises(ValueError): job_deleted(self.store,job,True)
        self.store.control(job,True)
        file=self.store.root/'jobs'/job/'result.txt';file.write_text('preserved')
        job_deleted(self.store,job,True)
        self.assertEqual(self.store.snapshot()['jobs'],[])
        self.assertEqual(file.read_text(),'preserved')
        with self.assertRaises(ValueError):run_job(self.store,job)
        job_deleted(self.store,job,False)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'],'paused')
        with (self.store.root/'runner.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ValueError):agent_deleted(self.store,aid,True)
    def test_endpoint_validation(self):
        self.assertEqual(endpoint('https://example.com','openai'),'https://example.com/v1/chat/completions')
        self.assertEqual(endpoint('https://example.com/v1/messages','anthropic'),'https://example.com/v1/messages')
        for u in ['http://example.com/v1','https://user:password@example.com','https://example.com?key=x','http://127.0.0.1:8765/v1']:
            with self.assertRaises(ValueError):endpoint(u,'openai')

class APITests(unittest.TestCase):
    plan = ManagementTests.plan
    def setUp(self):
        ManagementTests.setUp(self); self.requests=[]; outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path,dict(self.headers),body))
                if self.path.startswith('/redirect'):
                    self.send_response(302);self.send_header('Location','/v1/chat/completions');self.end_headers();return
                output='HUB_OK '+outer.config['key']
                payload={'model':'fixture-model','content':[{'type':'text','text':output}]} if self.path.endswith('/messages') else {'model':'fixture-model','choices':[{'message':{'content':output},'finish_reason':'stop'}]}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(payload).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def test_actual_worker_end_to_end_and_no_key_in_evidence(self):
        aid=save_agent(self.store,dict(self.config,endpoint=self.base+'/v1'))
        job=self.store.add_plan(self.plan(aid))
        self.assertEqual(run_job(self.store,job)['status'],'done')
        snapshot=json.dumps(self.store.snapshot())
        self.assertNotIn(self.config['key'],snapshot)
        self.assertIn('[REDACTED]',snapshot)
        self.assertEqual(self.requests[0][0],'/v1/chat/completions')
        self.assertEqual(self.requests[0][1]['Authorization'],'Bearer '+self.config['key'])
        for p in (self.store.root/'jobs').rglob('*'):
            if p.is_file():self.assertNotIn(self.config['key'],p.read_text())
    def test_anthropic_and_redirect_not_followed(self):
        aid=save_agent(self.store,dict(self.config,protocol='anthropic',endpoint=self.base+'/v1'))
        a=self.store.agents()[0]; result=infer(a,self.config['key'],'test')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.requests[-1][0],'/v1/messages')
        self.assertEqual(self.requests[-1][1]['X-Api-Key'],self.config['key'])
        a['endpoint']=self.base+'/redirect';before=len(self.requests)
        self.assertEqual(infer(a,self.config['key'],'test')['status'],'failed')
        self.assertEqual(len(self.requests),before+1)
    def test_pause_before_request_sends_nothing(self):
        from agent_hub.adapters import run_adapter
        aid=save_agent(self.store,dict(self.config,endpoint=self.base+'/v1'))
        attempt=self.store.root/'jobs'/'fixture'/'write'/'attempt-1'
        result=run_adapter(self.store.agents()[0],self.plan(aid)['tasks'][0],attempt/'workspace',attempt,lambda:True)
        self.assertEqual(result['status'],'paused');self.assertEqual(self.requests,[])

if __name__=='__main__':unittest.main()

class DashboardTests(unittest.TestCase):
    def test_management_http_actions_and_csrf(self):
        import re, socket, subprocess, sys, time, urllib.request, urllib.error
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'hub'; Store(root)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            proc=subprocess.Popen([sys.executable,'-B','-m','agent_hub','--root',str(root),'dashboard','--port',str(port),'--no-open'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            base='http://127.0.0.1:'+str(port)
            try:
                for _ in range(60):
                    try:
                        with urllib.request.urlopen(base,timeout=1) as r: page=r.read().decode()
                        break
                    except OSError:time.sleep(.05)
                else:self.fail('Dashboard did not start')
                token=(root/'.console-auth'/('%d.key'%port)).read_text()
                self.assertNotIn(token,page)
                self.assertNotIn('const token=',page)
                def post(data,secret=token):
                    request=urllib.request.Request(base+'/api/action',data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Agent-Hub-Token':secret,'Origin':base})
                    with urllib.request.urlopen(request,timeout=3) as r:return json.load(r)
                with self.assertRaises(urllib.error.HTTPError) as error:post({'action':'save_agent'},'wrong-token')
                self.assertEqual(error.exception.code,403)
                post({'action':'save_agent','config':{'id':'manual-fixture','name':'手工测试','transport':'file_bridge'}})
                post({'action':'create','goal':'功能测试','requirements':'验收','client':'manual-fixture'})
                store=Store(root)
                for _ in range(60):
                    jobs=store.snapshot()['jobs']
                    if jobs and jobs[0]['status']=='waiting_external':break
                    time.sleep(.05)
                self.assertEqual(jobs[0]['status'],'waiting_external');job=jobs[0]['id']
                post({'action':'delete_job','job':job});self.assertFalse(store.snapshot()['jobs'])
                post({'action':'restore_job','job':job});self.assertEqual(store.snapshot()['jobs'][0]['status'],'paused')
                post({'action':'delete_agent','agent':'manual-fixture'});self.assertFalse(store.agents())
                post({'action':'restore_agent','agent':'manual-fixture'});self.assertEqual(len(store.agents()),1)
            finally:
                proc.terminate();proc.wait(timeout=5)
