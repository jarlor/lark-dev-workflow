"""Offline project-actor and operator handoff checks."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill/lark-dev-workflow/scripts'))
import comments
import onboard
import unattended as runtime
import watch
from test_comments import FakeCli, config as project_config, reply


ACTOR = {'identity': 'bot', 'app_id': 'cli_example', 'open_id': 'ou_bot', 'profile': 'project-agent'}


class BotIdentity(unittest.TestCase):
    def runner(self, *, who=None, bot=None, response=None):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'whoami':
                result = {'profile': ACTOR['profile'], 'identity': 'bot', 'appId': ACTOR['app_id'], **(who or {})}
            elif argv[1] == 'api':
                result = {'ok': True, 'identity': 'bot', 'data': {'open_id': 'ou_bot'}} if bot is None else bot
            else:
                result = {'ok': True, 'identity': 'bot', 'data': {}} if response is None else response
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), '')
        return calls, run

    def test_comment_and_base_reads_pin_bot_and_binary(self):
        calls, runner = self.runner()
        reader = watch.Cli(ACTOR['profile'], runner, executable='/test/lark-cli', actor=ACTOR)
        reader.read('drive', '+list-comments', '--token', 'base-fixture')
        reader.base.read('+table-list', '--base-token', 'base-fixture')
        self.assertEqual([call[1] for call in calls], ['whoami', 'api', 'drive', 'base'])
        for index, argv in enumerate(calls):
            self.assertEqual(argv[0], '/test/lark-cli')
            self.assertEqual(argv[argv.index('--profile') + 1], ACTOR['profile'])
            if index:
                self.assertEqual(argv[argv.index('--as') + 1], 'bot')

    def test_wrong_actual_actor_cannot_reach_a_business_endpoint(self):
        for kwargs in ({'who': {'appId': 'cli_other'}}, {'who': {'identity': 'user'}},
                       {'who': {'profile': 'other'}}, {'bot': {'code': 0, 'bot': {'open_id': 'ou_other'}}},
                       {'bot': {'ok': False, 'data': {'open_id': 'ou_bot'}}}):
            with self.subTest(kwargs=kwargs):
                calls, runner = self.runner(**kwargs)
                with self.assertRaises(onboard.OnboardingError):
                    comments.Cli(ACTOR['profile'], runner, actor=ACTOR)
                self.assertFalse(any(call[1] not in ('api', 'whoami') for call in calls))

    def test_refuses_a_user_response_from_bot_read(self):
        calls, runner = self.runner(response={'ok': True, 'identity': 'user', 'data': {}})
        reader = comments.Cli(ACTOR['profile'], runner, actor=ACTOR)
        with self.assertRaises(onboard.OnboardingError):
            reader.read('drive', '+list-replies')
        self.assertEqual(len(calls), 3)

    def test_binding_blocks_browser_user_or_profile_fallback(self):
        binding = {'cli_actor': ACTOR, 'base': {'token': 'test'}, 'documents': {}}
        for config, project in (({'format_version': 1}, binding), ({'format_version': 2}, binding),
                                ({'format_version': 3, 'lark_profile': 'personal'}, binding),
                                ({'format_version': 3}, {})):
            with self.subTest(config=config):
                with self.assertRaises(runtime.control.ControlError):
                    runtime.access_context(config, project)
        with self.assertRaises(comments.CommentError):
            comments.current_profile(binding, 'personal')
        project_context = runtime.access_context(
            {'format_version': 3, 'lark_profile': ACTOR['profile'], 'lark_bin': '/bin/lark-cli'}, binding)
        self.assertEqual(project_context['identity'], 'bot')
        self.assertEqual(project_context['open_id'], ACTOR['open_id'])
        self.assertNotEqual(comments.binding_identity(binding),
                            comments.binding_identity(dict(binding, cli_actor=dict(ACTOR, open_id='ou_other'))))

    def test_malformed_actor_does_not_default_to_user(self):
        for actor in (None, {}, dict(ACTOR, identity='auto'), dict(ACTOR, profile='--as'),
                      dict(ACTOR, app_id=''), dict(ACTOR, open_id='')):
            with self.subTest(actor=actor), self.assertRaises(onboard.OnboardingError):
                comments.current_profile({'cli_actor': actor})

    def test_own_reply_is_thread_history_and_a_human_reply_stays_actionable(self):
        project = dict(project_config(), cli_actor=ACTOR)
        cli = FakeCli(replies={'commentA': [reply(author=ACTOR['open_id']), reply('human-reply')]})
        state, report = comments.scan(project, comments.empty_state(project), cli, include_resolved=True)
        self.assertEqual(report['pending_count'], 1)
        self.assertEqual([item['reply_id'] for item in comments.pending(state)], ['human-reply'])
        entries, _ = watch._comment_snapshot(state)
        self.assertEqual(len(entries), 2)
        marked = [v['content']['reply_id'] for v in entries.values() if v['content'].get('assistant_authored')]
        self.assertEqual(marked, ['replyRoot'])
