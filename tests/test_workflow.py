import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub.core import Store, run_job, retry_task
from agent_hub.workflow import create_flow, read_flow, parse_plan, parse_review


def agent(aid):
    return {'id': aid, 'name': aid, 'transport': 'codex_cli', 'execution_model': 'model-a',
            'model': {'value': 'model-a'}, 'capabilities': ['reasoning', 'writing_zh', 'coding', 'review', 'files_read', 'files_write'],
            'automation': {'status': 'verified'}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'workspace')
        self.store.init({'agents': [agent('gpt'), agent('worker'),
                                   {'id': 'offline', 'name': 'WorkBuddy', 'transport': 'file_bridge', 'model': {'value': 'K3'}}]})
        self.calls = []
        self.plan = {'analysis': 'Analyze the actual constraints and deliver a concrete grounded result.',
                     'steps': [{'id': 'step1', 'agent': 'worker', 'kind': 'writing_zh',
                                'prompt': 'Write concrete content', 'reason': 'Available writing capability; no ranking claim',
                                'dependencies': [], 'needs_files': False, 'artifacts': []}]}
        self.revise = False
        self.fail = False
        self.pause = False

    def create(self, **kw):
        return create_flow(self.store, dict(goal='Create a reusable workflow guide', requirements='Provide concrete steps and limitations',
                                            commander='gpt', max_steps=2, **kw))

    def adapter(self, a, task, workspace, directory, stopped):
        self.calls.append(task['id'])
        if self.pause and task['workflow_role'] == 'work':
            self.store.control(self.job, True)
            self.pause = False
            return {'status': 'paused', 'output': ''}
        if self.fail and task['workflow_role'] == 'work':
            return {'status': 'failed', 'output': '', 'error': 'Transport interruption, effects unknown'}
        role = task['workflow_role']
        if role == 'planning': output = json.dumps(self.plan)
        elif role == 'review':
            revise = self.revise and task['id'] == 'review-r0'
            output = json.dumps({'decision': 'revise' if revise else 'accept', 'reason': 'Concrete evidence was checked against requirements',
                                 'issues': [{'step': 'step1', 'instruction': 'Add a concrete example'}] if revise else []})
        else: output = 'Concrete final content with verified steps and explicit limitations.'
        artifacts = []
        if role == 'work' and self.plan['steps'][0]['needs_files']:
            (workspace / 'report.md').write_text('Verified actual file content')
            artifacts = ['report.md']
        return {'status': 'completed', 'output': output, 'artifacts': artifacts, 'transport': 'codex_cli',
                'model': {'value': 'model-a', 'source': 'fixture'}, 'is_test_fixture': True, 'elapsed_seconds': 0.01}

    def run_flow(self, job):
        with patch('agent_hub.core.run_adapter', side_effect=self.adapter):
            return run_job(self.store, job)

    def test_full_pipeline_persists_delivery_and_does_not_repeat_completed_work(self):
        job = self.create()
        self.assertEqual(self.run_flow(job)['status'], 'done')
        self.assertEqual(self.calls, ['planning', 'step1-r0', 'synthesis-r0', 'review-r0'])
        flow = read_flow(self.store, job)[1]
        self.assertFalse(flow['delivery']['independence']['different_model'])
        self.assertTrue(flow['delivery']['independence']['different_agent'])
        self.assertTrue((Path(flow['delivery']['directory']) / 'final.md').is_file())
        self.assertEqual(self.run_flow(job)['status'], 'done')
        self.assertEqual(len(self.calls), 4)

    def test_revision_reexecutes_rejected_work_then_synthesis_and_review(self):
        self.revise = True
        job = self.create()
        self.assertEqual(self.run_flow(job)['status'], 'done')
        self.assertEqual(self.calls[-3:], ['step1-r1', 'synthesis-r1', 'review-r1'])
        row = next(r for r in self.store.task_rows(job) if r['id'] == 'step1-r1')
        task = json.loads(row['spec'])
        self.assertIn('review-r0', task['dependencies'])
        self.assertIn('step1-r0', task['dependencies'])

    def test_rejection_limit_never_publishes_draft(self):
        self.revise = True
        job = self.create(max_revisions=0)
        self.assertEqual(self.run_flow(job)['status'], 'needs_attention')
        self.assertFalse((self.store.root / 'jobs' / job / 'delivery').exists())
        self.assertIn('limit_reason', read_flow(self.store, job)[1])

    def test_pause_resume_does_not_rerun_planning(self):
        self.pause = True; self.job = self.create()
        self.assertEqual(self.run_flow(self.job)['status'], 'paused')
        self.store.control(self.job, False)
        self.assertEqual(self.run_flow(self.job)['status'], 'done')
        self.assertEqual(self.calls.count('planning'), 1)

    def test_transport_error_requires_explicit_retry(self):
        self.fail = True
        job = self.create()
        self.assertEqual(self.run_flow(job)['status'], 'needs_attention')
        self.assertEqual(self.run_flow(job)['status'], 'needs_attention')
        self.assertEqual(self.calls.count('step1-r0'), 1)
        self.fail = False; retry_task(self.store, job, 'step1-r0')
        self.assertEqual(self.run_flow(job)['status'], 'done')

    def test_untrusted_plan_cannot_choose_offline_agents_or_expand_permissions(self):
        job = self.create(); config = read_flow(self.store, job)[0]
        for change in [{'agent': 'offline'}, {'dependencies': ['step1']}, {'needs_files': True},
                       {'agent': 'gpt'}, {'kind': 'execute_shell'}, {'artifacts': ['../outside']}]:
            bad = json.loads(json.dumps(self.plan)); bad['steps'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_plan(json.dumps(bad), config)
        with self.assertRaises(ValueError):
            self.create(participants=['offline'])

    def test_budget_is_hard_cap_even_after_manual_retry(self):
        self.revise = True
        job = self.create(max_calls=5)
        self.assertEqual(self.run_flow(job)['status'], 'needs_attention')
        self.assertEqual(len(self.calls), 5)
        self.run_flow(job)
        self.assertEqual(len(self.calls), 5)

    def test_artifacts_are_copied_verified_and_delivered(self):
        self.plan['steps'][0].update(needs_files=True, artifacts=['report.md'])
        job = self.create(files_allowed=True)
        self.assertEqual(self.run_flow(job)['status'], 'done')
        final = self.store.root / 'jobs' / job / 'delivery' / 'step1' / 'report.md'
        self.assertEqual(final.read_text(), 'Verified actual file content')

    def test_profile_change_blocks_existing_plan(self):
        job = self.create()
        with self.store.connect() as con:
            a = agent('worker'); a['execution_model'] = 'model-b'
            con.execute('UPDATE agents SET data=? WHERE id=?', (json.dumps(a), 'worker'))
        self.assertEqual(self.run_flow(job)['status'], 'needs_attention')
        self.assertEqual(self.calls, ['planning'])

    def test_invalid_review_cannot_accept_with_issues(self):
        for obj in [{'decision': 'accept', 'reason': 'Checked', 'issues': [{'step': 'step1', 'instruction': 'Fix'}]},
                    {'decision': 'revise', 'reason': 'Checked', 'issues': []},
                    {'decision': 'accept', 'reason': ''}]:
            with self.assertRaises(ValueError): parse_review(json.dumps(obj), ['step1'])

    def test_malformed_planning_is_repaired_once_and_bounded(self):
        job = self.create()
        def broken(a, task, workspace, directory, stopped):
            self.calls.append(task['id'])
            return {'status': 'completed', 'output': 'This is not the requested planning JSON', 'artifacts': []}
        with patch('agent_hub.core.run_adapter', side_effect=broken):
            self.assertEqual(run_job(self.store, job)['status'], 'needs_attention')
        self.assertEqual(self.calls, ['planning', 'planning'])
        self.assertEqual(read_flow(self.store, job)[1]['calls'], 2)

    def test_crash_after_task_completion_resumes_without_duplicate(self):
        job = self.create()
        self.run_flow(job)
        config, state = read_flow(self.store, job)
        state['phase'] = 'reviewing'
        from agent_hub.workflow import save_state
        save_state(self.store, job, state)
        self.assertEqual(self.run_flow(job)['status'], 'done')
        self.assertEqual(len(self.calls), 4)

    def test_api_auto_route_opt_out_and_deleted_agents_are_excluded(self):
        a = agent('api'); a.update(transport='http_api', auto_route=False)
        with self.store.connect() as con:
            con.execute('INSERT INTO agents VALUES(?,?)', ('api', json.dumps(a)))
        self.assertNotIn('api', read_flow(self.store, self.create())[0]['participants'])
        with self.assertRaises(ValueError): self.create(participants=['api'])

    def test_fixed_model_registration_does_not_verify_desktop_transport(self):
        from agent_hub.management import save_profile
        save_profile(self.store, 'offline', 'K3', 'Await task capability evaluation')
        a = next(a for a in self.store.agents() if a['id'] == 'offline')
        self.assertEqual(a['model']['value'], 'K3')
        self.assertNotIn('observed_model', a)
        self.assertNotIn('offline', read_flow(self.store, self.create())[0]['participants'])


if __name__ == '__main__': unittest.main()
