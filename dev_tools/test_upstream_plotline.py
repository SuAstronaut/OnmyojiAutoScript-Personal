"""Offline summon scene and real template matcher; no task/device imports."""
import ast
from enum import Enum
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from test_takeover_reconnect import methods

ROOT = Path(__file__).resolve().parents[1]


class PlotTests(unittest.TestCase):
    def scene_task(self, visible=(), colors=(), settlement=False):
        source = ast.parse((ROOT / 'tasks/Plotline/script_task.py').read_text(encoding='utf-8'))
        enum = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'PlotlineScene')
        ns = {'Enum': Enum}
        exec(compile(ast.Module(body=[enum], type_ignores=[]), '<scene-enum>', 'exec'), ns)
        ns['ExplorationAssets'] = NS(I_NORMAL_BATTLE_BUTTON='normal', I_BOSS_BATTLE_BUTTON='boss')
        cls = methods(ROOT / 'tasks/Plotline/script_task.py', 'ScriptTask', {'get_current_scene'}, ns)
        task = cls()
        for name in ('I_PAGE_MAIN', 'I_PAGE_EXP_BATTLE', 'I_CLICK_BATTLE', 'I_CLICK_TO_AUTO',
                     'I_PREPARE_HIGHLIGHT', 'I_PAGE_PRIVILEGES', 'I_PAGE_SUMMON', 'I_PAGE_SUMMON2',
                     'I_PAGE_SUMMON_2', 'I_PAGE_SUMMON_DRAW'):
            setattr(task, name, name)
        task.is_in_settlement = lambda: settlement
        task.click_dialogue_high = task.click_dialogue_low = lambda: False
        task.exploration_flag = False
        task.appear = lambda rule, **kwargs: rule in visible
        task.appear_rgb = lambda rule, **kwargs: rule in colors
        return task, ns['PlotlineScene']

    def test_old_summon_recognition_is_preserved(self):
        for colors, visible in ((['I_PAGE_SUMMON'], []), (['I_PAGE_SUMMON2'], []), ([], ['I_PAGE_SUMMON_2'])):
            with self.subTest(colors=colors, visible=visible):
                task, scene = self.scene_task(visible, colors)
                self.assertEqual(task.get_current_scene(), scene.PLOTLINE_SCENE_SUMMON)

    def test_draw_prompt_recognizes_changed_border_background(self):
        task, scene = self.scene_task(['I_PAGE_SUMMON_DRAW'])
        self.assertEqual(task.get_current_scene(), scene.PLOTLINE_SCENE_SUMMON)

    def test_non_summon_frame_is_unknown(self):
        task, scene = self.scene_task()
        with patch('time.time', side_effect=[100, 101, 106]):
            self.assertEqual(task.get_current_scene(), scene.PLOTLINE_SCENE_UNKNOWN)

    def test_settlement_and_main_keep_priority_over_draw_prompt(self):
        task, scene = self.scene_task(['I_PAGE_SUMMON_DRAW'], settlement=True)
        self.assertEqual(task.get_current_scene(), scene.PLOTLINE_SCENE_SETTLEMENT)
        task, scene = self.scene_task(['I_PAGE_SUMMON_DRAW', 'I_PAGE_MAIN'])
        self.assertEqual(task.get_current_scene(), scene.PLOTLINE_SCENE_MAIN)

    def test_resource_png_and_real_matcher(self):
        data = json.loads((ROOT / 'tasks/Plotline/res/image.json').read_text(encoding='utf-8'))
        spec = next(item for item in data if item['itemName'] == 'page_summon_draw')
        self.assertEqual(spec['roiBack'], '480,580,320,70')
        template = cv2.cvtColor(cv2.imread(str(ROOT / 'tasks/Plotline/res/page_summon_draw.png')), cv2.COLOR_BGR2RGB)
        self.assertEqual(template.shape, (30, 250, 3))
        cls = methods(ROOT / 'module/atom/image.py', 'RuleImage', {'corp', 'match'},
                      {'np': np, 'cv2': cv2, 'logger': Mock()})
        rule = cls()
        rule.roi_back, rule.roi_front, rule.image = (480, 580, 320, 70), [521, 600, 250, 30], template
        rule.threshold, rule.is_template_match, rule.debug_mode = 0.8, True, False
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.assertFalse(rule.match(frame))
        random = np.random.default_rng(123).integers(0, 256, frame.shape, dtype=np.uint8)
        self.assertFalse(rule.match(random))
        frame[600:630, 521:771] = template
        self.assertTrue(rule.match(frame))
        self.assertEqual(rule.roi_front[:2], [521, 600])

    def test_level_roi_kept(self):
        self.assertIn('roi=(7,20,28,24)', (ROOT / 'tasks/Plotline/assets.py').read_text(encoding='utf-8'))
        data = json.loads((ROOT / 'tasks/Plotline/res/ocr.json').read_text(encoding='utf-8'))
        self.assertEqual(data[0]['roiFront'], '7,20,28,24')


if __name__ == '__main__':
    unittest.main()
