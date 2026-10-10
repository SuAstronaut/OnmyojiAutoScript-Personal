"""Offline OCR boxes, local verification, refill and identity regressions."""
import ast
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from test_takeover_reconnect import methods

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tasks/Component/SwitchHelpShikigami/switch_help_shikigami.py'


def result(text, x=20, y=8, width=40, height=20, score=0.99):
    return NS(ocr_text=text, box=[[x, y], [x + width, y], [x + width, y + height], [x, y + height]], score=score)


def namespace(factory=None):
    ns = dict(re=re, math=math, cv2=cv2, SimpleNamespace=NS, RuleOcr=factory or Mock(),
              logger=Mock(), HELP_TARGET_COUNT=13, page_friends='friends', page_main='main',
              sleep=Mock(), GamePageUnknownError=RuntimeError, Path=Path)
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 or isinstance(node, ast.ClassDef) and node.name == 'HelpTargetReached']
    exec(compile(tree, str(SOURCE), 'exec'), ns)
    return ns


class OcrTests(unittest.TestCase):
    def locate(self, text, probes, roi=(244, 631, 846, 42), width=50):
        calls = []
        def factory(**kwargs):
            calls.append(kwargs)
            if isinstance(probes, Exception):
                return NS(detect_and_ocr=Mock(side_effect=probes))
            return NS(detect_and_ocr=lambda image: probes)
        ns = namespace(factory)
        ocr = NS(roi=roi, detect_and_ocr=Mock(return_value=[result(text, 100, 3, width, 22)]))
        return ns['locate_help_shikigami_anchor'](ocr, np.zeros((720, 1280, 3), dtype=np.uint8)), calls

    def test_complete_count_and_character_corrections(self):
        parse = namespace()['parse_help_count']
        for text, count in [('0/15', 0), ('2/15', 2), ('13/15', 13), ('15/15', 15),
                            ('O／lS', 0), ('I3 / IS', 13), ('普通副本：13/15', 13)]:
            with self.subTest(text=text):
                self.assertEqual(parse(text), count)

    def test_incomplete_invalid_and_multiple_labels_are_not_counts(self):
        parse = namespace()['parse_help_count']
        for text in ('215', '15', '/15', '16/15', '115/15', '13/150', '00/15',
                     '-1/15', '2/15 3/15', '2/15，3/15', '9999991'):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_missing_slash_requires_local_full_label(self):
        (roi, count), calls = self.locate('215', [result('2/15')])
        self.assertEqual(count, 2)
        self.assertGreater(roi[0], 244)
        self.assertTrue(all(call['roi'][2] <= 104 for call in calls))
        self.assertIsNone(self.locate('215', [result('215')])[0][1])

    def test_primary_complete_result_never_falls_back_after_probe_failure(self):
        for probes in ([], RuntimeError('OCR unavailable'), [result('3/15')], [result('2/15', score=0.4)]):
            with self.subTest(probes=probes):
                self.assertEqual(self.locate('2/15', probes)[0], ([0, 0, 0, 0], None))

    def test_known_ten_to_zero_misread_can_be_corrected(self):
        self.assertEqual(self.locate('10/15', [result('0/15')])[0][1], 0)

    def test_merged_long_row_extracts_only_total_fifteen(self):
        self.assertEqual(self.locate('15/30 15/30 2/15', [result('2/15')], width=240)[0][1], 2)
        ns = namespace()
        candidates = list(ns['_iter_help_candidates'](result('2/15 3/15', width=180)))
        self.assertEqual([count for _, count in candidates], [2, 3])
        ns['_probe_help_count'] = Mock(side_effect=[(2, [10, 10, 40, 20]), (3, [90, 10, 40, 20])])
        ocr = NS(roi=(0, 0, 300, 40), detect_and_ocr=Mock(return_value=[result('2/15 3/15', width=180)]))
        self.assertEqual(ns['locate_help_shikigami_anchor'](ocr, np.zeros((720, 1280, 3), dtype=np.uint8)), ([0, 0, 0, 0], None))

    def test_adjacent_same_line_fragments_are_combined(self):
        fragments = [result('2', 20, 8, 8, 18), result('/', 30, 9, 4, 18), result('15', 37, 8, 14, 18)]
        self.assertEqual(self.locate('/15', fragments)[0][1], 2)

    def test_fragment_rows_and_distant_cards_cannot_be_joined(self):
        for fragments in ([result('2', 2, 0, 8, 12), result('/15', 15, 20, 18, 12)],
                          [result('2', 2, 8, 8, 18), result('/15', 65, 8, 18, 18)],
                          [result('2/15', 1, 8, 28, 18), result('3/15', 65, 8, 28, 18)],
                          [result('2/15', width=200)], [result('2/15', score=float('nan'))]):
            with self.subTest(fragments=fragments):
                self.assertIsNone(self.locate('/15', fragments)[0][1])

    def test_no_candidate_uses_same_roi_scaled_and_maps_boxes_back(self):
        calls = []
        def factory(**kwargs):
            calls.append(kwargs)
            boxes = [result('2/15', 400, 12, 200, 88)] if kwargs['name'].endswith('enhanced') else [result('2/15')]
            return NS(detect_and_ocr=lambda image: boxes)
        ns = namespace(factory)
        ocr = NS(roi=(244, 631, 846, 42), detect_and_ocr=Mock(return_value=[]))
        roi, count = ns['locate_help_shikigami_anchor'](ocr, np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertEqual(count, 2)
        self.assertEqual(calls[0]['roi'], (0, 0, 3384, 168))
        self.assertGreater(roi[0], 244)

    def test_callers_sync_only_confirmed_counts_and_keep_separate_rois(self):
        ns = namespace()
        rule = object()
        task = NS(device=NS(image=object()), O_FIND_SHIKIGAMI_HELP=rule,
                  _help_target_enabled=True, _help_target_synced=False, current_count=4, _help_battle_limit=30)
        ns['locate_help_shikigami_anchor'] = Mock(return_value=([300, 630, 40, 20], 10))
        shared = methods(SOURCE, 'SwitchHelpShikigami', {'locate_help_shikigami'}, ns)
        shared.locate_help_shikigami(task)
        # current_count=4 含当前准备场预增，实际已完成 3 场；缺额 3，终点为 6。
        self.assertEqual(task._help_battle_limit, 6)
        self.assertEqual(task.current_count, 4)
        self.assertTrue(task._help_target_synced)
        task._help_target_synced = False
        ns['locate_help_shikigami_anchor'].return_value = ([0, 0, 0, 0], None)
        task._help_battle_limit = 30
        shared.locate_help_shikigami(task)
        self.assertEqual(task._help_battle_limit, 30)
        self.assertFalse(task._help_target_synced)
        master = object()
        ns['MasterDiscipleAssets'] = NS(O_FIND_SHIKIGAMI_HELP=master)
        daily = methods(ROOT / 'tasks/DailyAltAcc/alliedteam.py', 'Alliedteam', {'_locate_help_shikigami'}, ns)
        daily._locate_help_shikigami(task)
        ns['locate_help_shikigami_anchor'].assert_called_with(master, task.device.image)
        disciple = methods(ROOT / 'tasks/MasterDisciple/script_task.py', 'ScriptTask', {'_locate_help_shikigami'}, ns)
        disciple._locate_help_shikigami(task)
        ns['locate_help_shikigami_anchor'].assert_called_with(rule, task.device.image)
        resource = json.loads((ROOT / 'tasks/Component/SwitchHelpShikigami/res/ocr.json').read_text(encoding='utf-8'))
        self.assertEqual(resource[0]['roiFront'], '244,631,846,42')
        master_source = (ROOT / 'tasks/MasterDisciple/assets.py').read_text(encoding='utf-8')
        self.assertIn('roi=(228,605,874,87)', master_source)


class PrivateBusinessTests(unittest.TestCase):
    def test_refill_uses_actual_deficit_and_stops_at_thirteen(self):
        ns = namespace()
        task = NS(ui_goto=Mock(return_value=True))
        counts = iter([10, 12, 13])
        images = []
        def capture(task):
            task._friend_help_count = next(counts)
            images.append(object())
            return images[-1]
        ns['capture_friend_help_image'] = capture
        refill = Mock(return_value=True)
        self.assertIs(ns['capture_completed_help_image'](task, refill), images[-1])
        self.assertEqual([call.args[0] for call in refill.call_args_list], [3, 1])
        self.assertTrue(task._help_target_complete)

    def test_no_increment_blocked_and_uncertain_reading_stop_safely(self):
        for counts, refill_result, expected_calls in (([10, 10], True, 1), ([10, 11], False, 1), ([None], True, 0), ([10, None], True, 1)):
            with self.subTest(counts=counts, blocked=refill_result):
                ns, values = namespace(), iter(counts)
                task = NS(ui_goto=Mock(return_value=True))
                def capture(task):
                    task._friend_help_count = next(values)
                    return object()
                ns['capture_friend_help_image'] = capture
                refill = Mock(return_value=refill_result)
                ns['capture_completed_help_image'](task, refill)
                self.assertEqual(refill.call_count, expected_calls)
                self.assertIsNot(task._help_target_complete, True)

    def test_target_reached_does_not_turn_existing_count_into_new_statistics(self):
        ns = namespace()
        task = NS(_help_target_enabled=True, _help_target_synced=False, current_count=4)
        with self.assertRaises(ns['HelpTargetReached']):
            ns['sync_help_battle_target'](task, 13)
        self.assertEqual(task.current_count, 4)

    def test_friend_page_uncertain_count_retries_and_retains_third_frame(self):
        ns = namespace()
        ns['Timer'] = lambda limit: NS(start=lambda: NS(reached=lambda: False))
        ns['SwitchHelpShikigamiAssets'] = NS(I_FRIEND_HELP_FLAG='flag')
        ns['O_FRIEND_HELP_COUNT'] = NS(detect_text=Mock(side_effect=['215', '13/150', '13/15']))
        task = NS(screenshot=Mock(side_effect=lambda: object()), ui_goto=Mock(return_value=True), appear=Mock(return_value=True))
        image = ns['capture_friend_help_image'](task)
        self.assertIsNotNone(image)
        self.assertEqual(task._friend_help_count, 13)
        self.assertEqual([call.args[0] for call in task.ui_goto.call_args_list], ['friends', 'main', 'friends', 'main', 'friends'])

    def test_failed_friend_navigation_preserves_exception(self):
        ns = namespace()
        task = NS(screenshot=Mock(), ui_goto=Mock(return_value=False))
        with self.assertRaises(RuntimeError):
            ns['capture_friend_help_image'](task)

    def test_screenshot_identity_is_preserved(self):
        name = namespace()['build_help_screenshot_name']({'char': '角色', 'svr': '区服', 'acc': '账号', 'sys': '安卓'}, 'test', 'full')
        self.assertEqual(name, '角色_区服_账号_安卓')


if __name__ == '__main__':
    unittest.main()
