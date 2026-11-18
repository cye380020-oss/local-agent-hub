import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from agent_hub.core import Store
from agent_hub.management import save_agent
from agent_hub.desktop_clients import resolve_app, open_and_check

APPS=[{'path':'/Applications/Gemini.app','name':'Gemini','bundle_id':'com.google.GeminiMacOS','version':'1'}]
class DesktopClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'workspace')
        self.aid=save_agent(self.store,{'name':'Gemini','transport':'file_bridge'})
    def test_exact_match_and_explicit_binding_only(self):
        self.assertEqual(resolve_app({'name':'gemini'},APPS),APPS[0])
        self.assertEqual(resolve_app({'name':'任意别名','app_path':APPS[0]['path']},APPS),APPS[0])
        for a in [{'name':'Gem'},{'app_path':'/tmp/evil.app'},{'name':'Gemini','desktop_bundle_id':'different'}]:
            with self.assertRaises(ValueError):resolve_app(a,APPS)
        with self.assertRaises(ValueError):resolve_app({'name':'Gemini'},APPS+[{**APPS[0],'path':'/Users/test/Applications/Gemini.app'}])
    def test_binding_requires_installed_application(self):
        with patch('agent_hub.desktop_clients.installed_apps',return_value=APPS):
            save_agent(self.store,{'id':self.aid,'name':'Gemini','transport':'file_bridge','app_path':APPS[0]['path']})
            with self.assertRaises(ValueError):save_agent(self.store,{'id':self.aid,'name':'Gemini','transport':'file_bridge','app_path':'/tmp/evil.app'})
        self.assertEqual(self.store.agents()[0]['desktop_bundle_id'],APPS[0]['bundle_id'])
    def test_process_start_does_not_claim_model_or_task_verified(self):
        receipt={'started':True,'bundle_id':APPS[0]['bundle_id'],'app_path':APPS[0]['path'],'pid':1234}
        with patch('agent_hub.desktop_clients.installed_apps',return_value=APPS),patch('pathlib.Path.is_file',return_value=True),patch('agent_hub.desktop_clients.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(receipt),'')):
            result=open_and_check(self.store,self.aid)
        self.assertTrue(result['started']);self.assertFalse(result['task_verified'])
        a=self.store.agents()[0]
        self.assertEqual(a['automation']['status'],'not_verified')
        self.assertNotIn('observed_model',a)
        self.assertEqual(self.store.snapshot()['measurements'],[])
    def test_wrong_receipt_and_timeout_never_pass(self):
        with patch('agent_hub.desktop_clients.installed_apps',return_value=APPS),patch('pathlib.Path.is_file',return_value=True):
            with patch('agent_hub.desktop_clients.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps({'started':True,'bundle_id':'wrong','app_path':APPS[0]['path']}),'')):
                self.assertFalse(open_and_check(self.store,self.aid)['started'])
            with patch('agent_hub.desktop_clients.subprocess.run',side_effect=subprocess.TimeoutExpired('probe',15)):
                self.assertFalse(open_and_check(self.store,self.aid)['started'])
    def test_api_cannot_be_launched_as_desktop_app(self):
        aid=save_agent(self.store,{'name':'API','transport':'http_api','protocol':'openai','endpoint':'https://example.com/v1','model':'test'})
        with patch('agent_hub.desktop_clients.subprocess.run') as run:
            with self.assertRaises(ValueError):open_and_check(self.store,aid)
            run.assert_not_called()
