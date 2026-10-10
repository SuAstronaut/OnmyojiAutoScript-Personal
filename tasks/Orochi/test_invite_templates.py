"""Native invite-popup regressions; isolate source AST to avoid device imports."""
import ast
import copy
import json
from pathlib import Path
import unittest

import cv2
import numpy as np

from tasks.Orochi.test_check_lock_diagnostics import (
    Harness, RuleImage, StopLoop, load_nodes, source_node,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'dev_tools/fixtures/orochi_invite'
MANIFEST = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))
NAMES = {'I_I_DEFAULT', 'I_I_NO_DEFAULT', 'I_GI_SURE'}


def effective_assets():
    """Execute the actual class bindings with the original base order intact."""
    namespace = {'RuleImage': RuleImage}
    for path, name in [
        ('tasks/Component/GeneralInvite/assets.py', 'GeneralInviteAssets'),
        ('tasks/Orochi/assets.py', 'OrochiAssets'),
    ]:
        node = copy.deepcopy(source_node(path, name))
        node.body = [n for n in node.body if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Name) and n.targets[0].id in NAMES]
        node.body = node.body or [ast.Pass()]
        load_nodes([node], namespace)
    namespace['BaseTask'] = type('BaseTask', (), {})
    node = copy.deepcopy(source_node('tasks/Component/GeneralInvite/general_invite.py', 'GeneralInvite'))
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'check_and_invite', 'invite_again'}]
    import logging
    namespace['logger'] = logging.getLogger('offline_invite')
    load_nodes([node], namespace)
    node = copy.deepcopy(source_node('tasks/Orochi/script_task.py', 'ScriptTask'))
    for base in node.bases:
        if base.id not in namespace:
            namespace[base.id] = type(base.id, (), {})
    node.body = [n for n in node.body if isinstance(n, ast.Assign)
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in NAMES]
    node.body = node.body or [ast.Pass()]
    load_nodes([node], namespace)
    return namespace


def frame(label):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if label == 'closed':
        return image
    x, y, w, h = MANIFEST['roi']
    pixels = cv2.cvtColor(cv2.imread(str(FIXTURES / f'{label}.png')), cv2.COLOR_BGR2RGB)
    assert pixels.shape == (h, w, 3)
    image[y:y+h, x:x+w] = pixels
    return image


def resources(shared=False):
    namespace = effective_assets()
    cls = namespace['GeneralInviteAssets' if shared else 'ScriptTask']
    result = {name: getattr(cls, name) for name in NAMES}
    for rule in result.values():
        rule.file = str(ROOT / rule.file)
    return result


class InviteReplay(Harness):
    def __init__(self, initial, labels):
        super().__init__(labels)
        self.device.image = frame(initial)
        namespace = effective_assets()
        for name, rule in resources().items():
            original = rule.match

            def tracked(image, threshold=None, name=name, original=original):
                self.trace.append(('match', name))
                return original(image, threshold)

            rule.match = tracked
            setattr(self, name, rule)
        for method in ('check_and_invite', 'invite_again'):
            setattr(self, method, getattr(namespace['ScriptTask'], method).__get__(self))

    def screenshot(self):
        self.trace.append(('screenshot',))
        self.clock.now += self.step
        try:
            label = next(self.states)
        except StopIteration:
            raise StopLoop('simulated screenshot watchdog')
        self.device.image = frame(label)


class InviteTemplateTests(unittest.TestCase):
    def test_actual_screenshot_states_two_by_two_exclusive(self):
        for label, expected in [('screenshot1', 'I_I_NO_DEFAULT'),
                                ('screenshot2', 'I_I_DEFAULT')]:
            for name, rule in resources().items():
                if name == 'I_GI_SURE':
                    continue
                with self.subTest(image=label, template=name):
                    self.assertEqual(rule.match(frame(label)), name == expected)
                    if name == expected:
                        self.assertGreater(rule.last_match_score, .99)
                        self.assertEqual(rule.roi_front, [546, 346, 30, 30])
                    else:
                        self.assertLess(rule.last_match_score, .6)

    def test_confirmation_detects_both_real_states(self):
        for label in ['screenshot1', 'screenshot2']:
            rule = resources()['I_GI_SURE']
            self.assertTrue(rule.match(frame(label)))
            self.assertGreater(rule.last_match_score, .99)
            self.assertEqual(rule.roi_front, [670, 402, 175, 60])

    def test_orochi_bindings_win_without_changing_shared_component(self):
        namespace = effective_assets()
        for name in NAMES:
            self.assertIs(getattr(namespace['ScriptTask'], name),
                          getattr(namespace['OrochiAssets'], name))
            self.assertIn('/tasks/Component/GeneralInvite/',
                          '/' + getattr(namespace['GeneralInviteAssets'], name).file)
            self.assertIsNot(getattr(namespace['ScriptTask'], name),
                             getattr(namespace['GeneralInviteAssets'], name))

    def test_threshold_search_and_control_names_preserved(self):
        metadata = json.loads((ROOT / 'tasks/Orochi/o/image.json').read_text(encoding='utf-8'))
        for name, rule in resources().items():
            old = resources(shared=True)[name]
            self.assertEqual(rule.threshold, .8)
            self.assertEqual(rule.roi_back, old.roi_back)
            self.assertEqual(rule.name, old.name)
            item = next(n for n in metadata if n['itemName'].upper() == name[2:])
            self.assertEqual(tuple(map(int, item['roiBack'].split(','))), rule.roi_back)
            self.assertEqual(tuple(map(int, item['roiFront'].split(','))), tuple(rule.roi_front))

    def test_no_other_popup_element_is_a_passing_candidate(self):
        x, y, w, h = MANIFEST['roi']
        for label in ['screenshot1', 'screenshot2']:
            for name, rule in resources().items():
                res = cv2.matchTemplate(frame(label)[y:y+h, x:x+w], rule.image,
                                        cv2.TM_CCOEFF_NORMED)
                expected = (670, 402) if name == 'I_GI_SURE' else (546, 346)
                wrong_state = ((name == 'I_I_DEFAULT' and label == 'screenshot1')
                               or (name == 'I_I_NO_DEFAULT' and label == 'screenshot2'))
                locations = np.argwhere(res > rule.threshold)
                if wrong_state:
                    self.assertEqual(len(locations), 0)
                for row, col in locations:
                    self.assertLessEqual(abs(int(col) + x - expected[0]), 2)
                    self.assertLessEqual(abs(int(row) + y - expected[1]), 2)

    def test_unchecked_is_clicked_then_confirmed_with_original_intervals(self):
        task = InviteReplay('screenshot1', ['screenshot1', 'screenshot2', 'screenshot2', 'closed'])
        self.assertTrue(task.check_and_invite(True))
        clicks = [n for n in task.trace if n[0] == 'click']
        self.assertEqual([dict(n[3])['control_name'] for n in clicks],
                         ['GI_I_NO_DEFAULT', 'GI_GI_SURE'])
        self.assertTrue(546 <= clicks[0][1] < 576 and 346 <= clicks[0][2] < 376)
        self.assertEqual(sum(n[0] == 'screenshot' for n in task.trace), 4)
        self.assertEqual(set(task.interval_timer), {'GI_I_NO_DEFAULT', 'GI_GI_SURE'})
        self.assertTrue(all(t.limit == 1 for t in task.interval_timer.values()))

    def test_already_checked_confirms_without_toggling(self):
        task = InviteReplay('screenshot2', ['screenshot2', 'screenshot2', 'closed'])
        self.assertTrue(task.check_and_invite(True))
        clicks = [n for n in task.trace if n[0] == 'click']
        self.assertEqual([dict(n[3])['control_name'] for n in clicks], ['GI_GI_SURE'])
        self.assertEqual(sum(n[0] == 'screenshot' for n in task.trace), 3)

    def test_false_configuration_keeps_original_no_toggle_semantics(self):
        # check_and_invite(False) confirms as-is; it does not actively uncheck.
        for label in ['screenshot1', 'screenshot2']:
            task = InviteReplay(label, [label, 'closed'])
            self.assertTrue(task.check_and_invite(False))
            self.assertFalse(any(n[0] == 'match' and n[1] in {'I_I_DEFAULT', 'I_I_NO_DEFAULT'}
                                 for n in task.trace))
            self.assertEqual(sum(n[0] == 'click' for n in task.trace), 1)

    def test_no_popup_returns_without_screenshot_or_click(self):
        task = InviteReplay('closed', [])
        self.assertFalse(task.check_and_invite(True))
        self.assertFalse(any(n[0] in {'screenshot', 'click'} for n in task.trace))

    def test_slow_toggle_keeps_one_second_click_gate(self):
        task = InviteReplay('screenshot1', ['screenshot1'] * 4 + ['screenshot2', 'screenshot2', 'closed'])
        self.assertTrue(task.check_and_invite(True))
        self.assertEqual(sum(n[0] == 'click' and dict(n[3])['control_name'] == 'GI_I_NO_DEFAULT'
                             for n in task.trace), 1)

    def test_invite_again_false_still_unchecks_then_confirms(self):
        task = InviteReplay('screenshot2', ['screenshot2', 'screenshot2', 'screenshot1', 'screenshot1', 'closed'])
        self.assertIsNone(task.invite_again(False))
        self.assertEqual([dict(n[3])['control_name'] for n in task.trace if n[0] == 'click'],
                         ['GI_I_DEFAULT', 'GI_GI_SURE'])


if __name__ == '__main__':
    unittest.main()
