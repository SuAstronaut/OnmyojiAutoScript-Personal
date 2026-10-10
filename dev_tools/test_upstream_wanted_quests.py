"""Replay exploration frames with the production Timer and no device imports."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from test_takeover_reconnect import Clock, methods

ROOT = Path(__file__).resolve().parents[1]
Scene = NS(ENTRANCE='entrance', WORLD='world', MAIN='main', BATTLE_PREPARE='prepare', UNKNOWN='unknown')


class FramesFinished(Exception):
    pass


class ExploreWantedBoss(Exception):
    pass


class Replay:
    def __init__(self, frames):
        self.clock, self.frames, self.frame, self.events = Clock(), iter(frames), None, []
        tree = ast.parse((ROOT / 'module/base/timer.py').read_text(encoding='utf-8'))
        timer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Timer')
        namespace = {'time': NS(time=self.clock.time)}
        exec(compile(ast.Module(body=[timer], type_ignores=[]), '<timer>', 'exec'), namespace)
        cls = methods(ROOT / 'tasks/WantedQuests/explore.py', 'WQExplore', {'explore'},
                      {'Timer': namespace['Timer'], 'Scene': Scene, 'RuleImage': object,
                       'ExploreWantedBoss': ExploreWantedBoss, 'logger': Mock()})
        self.task = cls()
        for name in ('I_UI_BACK_YELLOW', 'I_E_EXPLORATION_CLICK', 'I_CHECK_EXPLORATION',
                     'I_E_SETTINGS_BUTTON', 'I_BATTLE_REWARD', 'I_BOSS_BATTLE_BUTTON',
                     'TEMPLATE_GIF', 'S_SWIPE_BACKGROUND_RIGHT', 'I_PREPARE_HIGHLIGHT'):
            setattr(self.task, name, name)
        self.task.device, self.task.minions_cnt = NS(image=object()), 0
        self.task.get_current_scene = self.scene
        self.task.appear = self.appear
        self.task.fire = self.fire
        self.task.swipe = lambda *args, **kwargs: self.events.append(('swipe', self.clock.now))
        self.task.ui_get_reward = self.reward
        self.task._match_end = NS(stable=lambda *args, **kwargs: self.frame.get('end', False))
        self.task.screenshot = self.task.appear_then_click = self.task.ui_click = Mock()
        self.task.wait_until_stable = self.task.ui_click_until_disappear = Mock()
        self.task.quit_explore = Mock()

    def scene(self, **kwargs):
        try:
            self.frame = next(self.frames)
        except StopIteration:
            raise FramesFinished
        self.clock.now = self.frame['at']
        return self.frame.get('scene', Scene.MAIN)

    def appear(self, rule):
        if self.frame is None:
            return rule in ('I_UI_BACK_YELLOW', 'I_E_EXPLORATION_CLICK')
        return rule in self.frame.get('visible', ())

    def fire(self, rule):
        self.clock.sleep(self.frame.get('cost', 0))
        return self.frame.get('fire', False)

    def reward(self, rule):
        self.clock.sleep(self.frame.get('cost', 0))
        return True

    def run_until_exhausted(self):
        with unittest.TestCase().assertRaises(FramesFinished):
            self.task.explore('goto', 9)


class ExplorationTests(unittest.TestCase):
    def test_early_world_return_finishes_batch_after_target(self):
        replay = Replay([{'at': 100, 'visible': ['TEMPLATE_GIF'], 'fire': True},
                         {'at': 101, 'scene': Scene.WORLD}, {'at': 102, 'scene': Scene.WORLD}])
        replay.task.explore('goto', 9)
        self.assertFalse(replay.events)

    def test_empty_batch_at_world_rejects_boss_route(self):
        replay = Replay([{'at': 100, 'scene': Scene.WORLD}, {'at': 101, 'scene': Scene.WORLD}])
        with self.assertRaises(ExploreWantedBoss):
            replay.task.explore('goto', 9)

    def test_slow_frames_still_require_three_misses(self):
        replay = Replay([{'at': 100}, {'at': 105}, {'at': 110}, {'at': 111}, {'at': 116}])
        replay.run_until_exhausted()
        self.assertEqual(replay.events, [('swipe', 110)])

    def test_highlight_seen_but_click_not_ready_clears_failures(self):
        replay = Replay([{'at': 100}, {'at': 105}, {'at': 106, 'visible': ['TEMPLATE_GIF']},
                         {'at': 120}, {'at': 125}, {'at': 130}])
        replay.run_until_exhausted()
        self.assertEqual(replay.events, [('swipe', 130)])

    def test_battle_reward_and_transition_start_fresh_window(self):
        for event in ({'visible': ['I_BOSS_BATTLE_BUTTON'], 'fire': True, 'cost': 30},
                      {'visible': ['I_BATTLE_REWARD'], 'cost': 30},
                      {'scene': Scene.BATTLE_PREPARE}, {'scene': Scene.UNKNOWN}):
            with self.subTest(event=event):
                replay = Replay([{'at': 100}, {'at': 105}, {'at': 106, **event},
                                 {'at': 140}, {'at': 145}, {'at': 150}])
                replay.run_until_exhausted()
                self.assertEqual(replay.events, [('swipe', 150)])

    def test_end_detection_waits_for_confirmed_misses(self):
        replay = Replay([{'at': 100, 'end': True}, {'at': 105, 'end': True},
                         {'at': 110, 'end': True}])
        replay.run_until_exhausted()
        replay.task.quit_explore.assert_called_once()
        self.assertFalse(replay.events)


if __name__ == '__main__':
    unittest.main()
