import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service' / 'linux'))
from check_schedule import verify


class Cutover(unittest.TestCase):
    def test_legacy_custom_wrapper_and_independent_project(self):
        legacy = '/private/old runner.json'
        marker = hashlib.sha256(str(Path(legacy).resolve()).encode()).hexdigest()[:16]
        bridge = {'mode': 'dispatch', 'runner_config': '/private/bot.json'}
        for cron in [f'* * * * * /custom/tick # lark-dev-workflow:{marker}',
                     f'*/10 * * * * /custom/tick --config "{legacy}"',
                     '*/10 * * * * /bin/sh /custom/tick --config=/private/bot.json']:
            with self.assertRaises(ValueError):
                verify(bridge, [legacy], cron)
        verify(bridge, [legacy], f'# * * * * * /old # lark-dev-workflow:{marker}\n* * * * * /other/tick')
        verify({'mode': 'observe'}, [], f'* * * * * /old # lark-dev-workflow:{marker}')
