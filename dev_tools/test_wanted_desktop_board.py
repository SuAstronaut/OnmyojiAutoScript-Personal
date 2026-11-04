"""Offline desktop wanted-board regression tests; never connects to a device.

Compile production image/board classes without importing runtime device/config
modules. Jade/cat and card-relocation cases are explicitly simulated.
"""
import ast
import copy
import re
import unittest
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'dev_tools/fixtures/wanted_v2'


def production_board():
    ns = dict(cv2=cv2, np=np, float32=np.float32, int32=np.int32,
              uint8=np.uint8, fromfile=np.fromfile, Path=Path,
              cached_property=cached_property, copy=copy,
              RuleImageMallResourceMixin=object, logger=Mock(),
              get_current_humanizer=lambda: None)
    path = ROOT / 'module/atom/image.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tree.body = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RuleImage']
    exec(compile(tree, str(path), 'exec'), ns)
    path = ROOT / 'tasks/DailyAltAcc/wanted_board.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, str(path), 'exec'), ns)
    return ns['WantedBoardV2']


def frame(name):
    raw = cv2.imdecode(np.fromfile(str(FIXTURES / name), np.uint8), 1)
    return cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)


class DesktopBoardPixels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = production_board()

    def test_native_desktop_captures(self):
        for name, real, kind in [('desktop_real_gold.png', True, 'gold'),
                                 ('desktop_normal_sushi.png', False, 'sushi'),
                                 ('desktop_normal_dog_food.png', False, 'dog_food')]:
            with self.subTest(name=name):
                image = frame(name)
                self.assertTrue(self.board.is_open(image))
                self.assertTrue(self.board.ready(image))
                cards = self.board.scan(image)
                self.assertEqual([(c['slot'], c['real'], c['type_name']) for c in cards],
                                 [(0, real, kind)])
                self.assertEqual(cards[0]['inviteBtn'].roi_front, [192, 400, 99, 30])

    def test_desktop_cards_in_all_four_slots_and_after_invite(self):
        # Simulated relocation/removal; no additional real-game captures.
        for name in ('desktop_real_gold.png', 'desktop_normal_sushi.png',
                     'desktop_normal_dog_food.png'):
            source = frame(name)
            expected = self.board.scan(source)[0]
            for slot, left in enumerate((110, 378, 646, 914)):
                for accepted in (False, True):
                    with self.subTest(name=name, slot=slot, accepted=accepted):
                        image = source.copy()
                        image[125:620, 110:1170] = 0
                        image[125:620, left:left+256] = source[125:620, 110:366]
                        if accepted:
                            image[388:446, left:left+256] = 0
                        cards = self.board.scan(image)
                        self.assertEqual([(c['slot'], c['real'], c['type_name']) for c in cards],
                                         [(slot, expected['real'], expected['type_name'])])
                        if accepted:
                            self.assertIsNone(cards[0]['inviteBtn'])
                        else:
                            self.assertEqual(cards[0]['inviteBtn'].roi_front[0], left+82)

    def test_desktop_real_sushi_jade_and_normal_cat_compatibility(self):
        # Simulations using reward strips from earlier native captures, scaled
        # to desktop artwork size. These are not real desktop jade/cat samples.
        for source_name, real, kind in [('real_cooperation.png', True, 'sushi'),
                                        ('jade_with_mystery.png', True, 'jade'),
                                        ('double_cooperation.png', False, 'cat_food')]:
            with self.subTest(kind=kind):
                image = frame('desktop_real_gold.png' if real else 'desktop_normal_sushi.png')
                reward = cv2.resize(frame(source_name)[534:608, 140:304], None, fx=.95, fy=.95)
                image[526:609, 130:355] = 0
                h, w = reward.shape[:2]
                image[526:526+h, 165:165+w] = reward
                cards = self.board.scan(image)
                self.assertEqual([(c['real'], c['type_name']) for c in cards], [(real, kind)])

    def test_desktop_missing_marker_stays_unknown_and_ordinary_rewards_are_ignored(self):
        image = frame('desktop_real_gold.png')
        image[294:352, 120:175] = 0
        cards = self.board.scan(image)
        self.assertEqual(len(cards), 1)
        self.assertIsNone(cards[0]['real'])
        image[388:446, 110:366] = 0
        self.assertEqual(self.board.scan(image), [])

    def test_desktop_requires_native_resolution(self):
        self.assertFalse(self.board.is_open(cv2.resize(frame('desktop_normal_sushi.png'), (160, 90))))

class TargetRegions(unittest.TestCase):
    def test_production_ocr_uses_current_layout(self):
        board = production_board()
        source = ROOT / 'tasks/DailyAltAcc/cooperation.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        coop = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Cooperation')
        parsers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith('_parse_')]
        reader = next(n for n in coop.body if isinstance(n, ast.FunctionDef) and n.name == '_read_cooperation_targets_v2')
        ns = dict(WantedBoardV2=board, re=re, logger=Mock())
        exec(compile(ast.Module(body=parsers+[reader], type_ignores=[]), str(source), 'exec'), ns)
        for image_name, real, expected_y in [('desktop_real_gold.png', True, 453),
                                             ('real_cooperation.png', True, 460),
                                             ('desktop_normal_sushi.png', False, 432)]:
            for slot, left in enumerate((110,378,646,914) if image_name.startswith('desktop') else (90,373,655,938)):
                with self.subTest(image=image_name, real=real, slot=slot):
                    ocr = Mock(return_value='击败2个鲤鱼精')
                    task = SimpleNamespace(COOP_TARGET_READ={('sushi',real):'real' if real else 'normal'},
                                           _ocr_single=ocr, _normal_target_result=Mock(return_value={}))
                    result = ns['_read_cooperation_targets_v2'](task,frame(image_name),'sushi',real,slot)
                    self.assertEqual(ocr.call_args_list[0].args[1],(left+20,expected_y,225,25))
                    if real:
                        self.assertEqual(result,{'monster_text':'鲤鱼精'})
                        self.assertEqual(ocr.call_count,1)
                    else:
                        self.assertEqual(ocr.call_args_list[1].args[1],(left+20,477,225,25))
                        self.assertEqual(ocr.call_count,2)

    def test_legacy_boards_keep_original_classification(self):
        board=production_board()
        for name, expected in [('real_cooperation.png',[(0,True,'sushi')]),
                               ('jade_with_mystery.png',[(0,False,'jade')]),
                               ('double_cooperation.png',[(0,False,'cat_food'),(1,False,'gold')])]:
            with self.subTest(name=name):
                self.assertEqual([(c['slot'],c['real'],c['type_name']) for c in board.scan(frame(name))],expected)


if __name__ == '__main__':
    unittest.main()
