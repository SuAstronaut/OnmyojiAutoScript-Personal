"""Offline EvoZone cleanup, throttling, counters and stamina regressions."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from test_takeover_reconnect import methods

ROOT = Path(__file__).resolve().parents[1]


class Replay:
    def __init__(self, frames):
        cls = methods(ROOT / 'tasks/EvoZone/script_task.py', 'ScriptTask', {'run_alone'},
                      {'logger': Mock(), 'datetime': NS(now=lambda: datetime(2026, 10, 10)),
                       'page_awake_zones': 'awake', 'page_main': 'main',
                       'HelpTargetReached': type('HelpTargetReached', (Exception,), {}),
                       'GamePageUnknownError': RuntimeError})
        self.task, self.frames, self.visible, self.events = cls(), iter(frames), set(), []
        t = self.task
        for name in ('I_EVOZONE_FIRE', 'I_PET_PRESENT', 'C_WIN_3', 'I_EXIT_ENSURE',
                     'I_FALSE', 'I_EVOZONE_BUY_AP_CLOSE', 'I_FORM_TEAM', 'I_BACK_Y'):
            setattr(t, name, name)
        t.config = NS(evo_zone=NS(evo_zone_config=NS(layer=10),
                                 general_battle_config=NS(lock_team_enable=True)))
        t.current_count, t.limit_count, t._help_battle_limit = 0, 1, 1
        t._help_target_enabled = t._help_battle_count_restored = t._help_battle_blocked = False
        t.start_time, t.limit_time = datetime(2026, 10, 10), timedelta(hours=1)
        t.ui_get_current_page = t.ui_goto = t.evozone_enter = t.check_layer = Mock()
        t.check_lock = t.enable_help_shikigami_switch = t._restore_battle_count = Mock()
        t._is_help_shikigami_account = lambda: False
        t.screenshot, t.appear, t.appear_then_click = self.screenshot, self.appear, self.click
        t._persist_battle_count = Mock()
        t.run_general_battle = self.battle
        t.ui_click_until_disappear = Mock()

    def screenshot(self):
        self.visible = set(next(self.frames))

    def appear(self, rule, **kwargs):
        return rule in self.visible

    def click(self, rule, **kwargs):
        if rule not in self.visible:
            return False
        self.events.append(rule)
        return 'throttled' not in self.visible

    def battle(self, **kwargs):
        self.events.append('battle')
        self.task.current_count += 1


READY = ['I_EVOZONE_FIRE']


class EvoTests(unittest.TestCase):
    def run_battle(self, prefix):
        replay = Replay(prefix + [READY, READY, [], READY, []])
        self.assertTrue(replay.task.run_alone())
        self.assertEqual(replay.task.current_count, 1)
        replay.task._persist_battle_count.assert_called_once()
        return replay

    def test_normal_settlement_preserves_actual_battle_and_progress(self):
        replay = self.run_battle([])
        self.assertEqual(replay.events, ['I_EVOZONE_FIRE', 'battle'])

    def test_late_exit_dialog_cleared_before_challenge(self):
        replay = self.run_battle([['I_EXIT_ENSURE', 'I_EVOZONE_FIRE']])
        self.assertEqual(replay.events[:2], ['I_EXIT_ENSURE', 'I_EVOZONE_FIRE'])

    def test_failure_settlement_cleared_before_challenge(self):
        replay = self.run_battle([['I_FALSE', 'I_EVOZONE_FIRE']])
        self.assertEqual(replay.events[:2], ['I_FALSE', 'I_EVOZONE_FIRE'])

    def test_throttled_cleanup_does_not_click_background(self):
        replay = self.run_battle([['I_EXIT_ENSURE', 'I_EVOZONE_FIRE', 'throttled'],
                                  ['I_EXIT_ENSURE', 'I_EVOZONE_FIRE'],
                                  ['I_FALSE', 'I_EVOZONE_FIRE', 'throttled']])
        self.assertEqual(replay.events[:4], ['I_EXIT_ENSURE', 'I_EXIT_ENSURE', 'I_FALSE', 'I_EVOZONE_FIRE'])

    def test_stamina_shortage_preserves_blocked_result_without_battle(self):
        replay = Replay([READY, ['I_EVOZONE_BUY_AP_CLOSE', 'I_EVOZONE_FIRE'], []])
        replay.task._help_target_enabled = True
        self.assertFalse(replay.task.run_alone())
        self.assertEqual(replay.task.current_count, 0)
        self.assertFalse(replay.events)
        replay.task._persist_battle_count.assert_not_called()
        replay.task.ui_click_until_disappear.assert_called_once_with('I_EVOZONE_BUY_AP_CLOSE', interval=1)


if __name__ == '__main__':
    unittest.main()
