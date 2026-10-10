"""Real screenshot ROI regressions; AST isolation prevents all device imports."""
import ast
import json
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tasks.Orochi.test_check_lock_diagnostics import Harness, RuleImage, StopLoop


FIXTURES = ROOT / 'dev_tools/fixtures/orochi_lock'
MANIFEST = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))


def specifications(path, names):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    return {
        n.targets[0].id: {k.arg: ast.literal_eval(k.value) for k in n.value.keywords}
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id in names and isinstance(n.value, ast.Call)
    }


SPEC = specifications('tasks/Orochi/assets.py', {'I_OROCHI_LOCK', 'I_OROCHI_UNLOCK'})


def rules():
    return {name: RuleImage(**dict(values, file=str(ROOT / values['file'])))
            for name, values in SPEC.items()}


def frame(label):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y, w, h = MANIFEST['roi']
    pixels = cv2.cvtColor(cv2.imread(str(FIXTURES / f'{label}.png')), cv2.COLOR_BGR2RGB)
    assert pixels.shape == (h, w, 3)
    image[y:y+h, x:x+w] = pixels
    return image


class Replay(Harness):
    def __init__(self, labels, baseline=False):
        super().__init__(labels, baseline=baseline)
        for name, values in SPEC.items():
            rule = getattr(self, name)
            rule.roi_front = list(values['roi_front'])
            rule.roi_back = values['roi_back']
            rule.threshold = values['threshold']

    def screenshot(self):
        self.trace.append(('screenshot',))
        self.clock.now += self.step
        try:
            label = next(self.states)
        except StopIteration:
            raise StopLoop('simulated screenshot watchdog')
        self.device.image = frame(label)


class LockTemplateTests(unittest.TestCase):
    def test_mumu_two_by_two_exclusive_at_actual_lock(self):
        for label, expected in [('mumu_A', 'I_OROCHI_LOCK'), ('mumu_B', 'I_OROCHI_UNLOCK')]:
            for name, rule in rules().items():
                with self.subTest(image=label, template=name):
                    result = rule.match(frame(label))
                    self.assertEqual(result, name == expected)
                    if result:
                        self.assertGreater(rule.last_match_score, 0.98)
                        self.assertEqual(rule.roi_front, [700, 658, 30, 22])
                    else:
                        self.assertLess(rule.last_match_score, 0.5)

    def test_desktop_locked_positive_and_unlock_negative(self):
        resource = rules()
        self.assertTrue(resource['I_OROCHI_LOCK'].match(frame('desktop_lock')))
        self.assertGreater(resource['I_OROCHI_LOCK'].last_match_score, 0.98)
        self.assertEqual(resource['I_OROCHI_LOCK'].roi_front, [700, 658, 30, 22])
        self.assertFalse(resource['I_OROCHI_UNLOCK'].match(frame('desktop_lock')))

    def test_metadata_dimensions_threshold_and_search_unchanged(self):
        data = json.loads((ROOT / 'tasks/Orochi/o/image.json').read_text(encoding='utf-8'))
        for name, rule in rules().items():
            self.assertEqual(rule.image.shape, (22, 30, 3))
            self.assertEqual(SPEC[name]['roi_front'], (700, 658, 30, 22))
            self.assertEqual(rule.threshold, 0.7)
            self.assertEqual(rule.roi_back, (613, 643, 293, 73) if name.endswith('_LOCK') else (628, 639, 178, 73))
            item = next(n for n in data if n['itemName'].upper() == name[2:])
            self.assertEqual(tuple(map(int, item['roiFront'].split(','))), SPEC[name]['roi_front'])
            self.assertEqual(tuple(map(int, item['roiBack'].split(','))), rule.roi_back)
            self.assertEqual(item['threshold'], rule.threshold)

    def test_lock_already_satisfied_returns_after_one_screenshot_no_click(self):
        for label in ['mumu_A', 'desktop_lock']:
            with self.subTest(environment=label):
                task = Replay([label])
                self.assertIs(task.run()[0], True)
                self.assertEqual(sum(n[0] == 'screenshot' for n in task.trace), 1)
                self.assertEqual(sum(n[0] == 'click' for n in task.trace), 0)
                self.assertNotIn(('match', 'OROCHI_UNLOCK'), task.trace)
                self.assertTrue(any('LOCK_success_return' in m for m in task.messages))

    def test_unlocked_clicks_original_rule_then_returns_when_locked(self):
        task = Replay(['mumu_B', 'mumu_A'])
        self.assertIs(task.run()[0], True)
        clicks = [n for n in task.trace if n[0] == 'click']
        self.assertEqual(len(clicks), 1)
        self.assertEqual(dict(clicks[0][3])['control_name'], 'OROCHI_UNLOCK')
        self.assertTrue(700 <= clicks[0][1] < 730 and 658 <= clicks[0][2] < 680)
        self.assertEqual(sum(n[0] == 'screenshot' for n in task.trace), 2)
        self.assertEqual([n[1] for n in task.trace if n[0] == 'match'],
                         ['OROCHI_LOCK', 'OROCHI_UNLOCK', 'OROCHI_LOCK'])

    def test_real_replay_preserves_timer_rng_and_click_trace(self):
        for labels, expected_lock in [(['mumu_A'], True), (['mumu_B', 'mumu_A'], True),
                                     (['mumu_B', 'mumu_B', 'mumu_A'], True),
                                     (['mumu_A', 'mumu_B'], False)]:
            with self.subTest(labels=labels, lock=expected_lock):
                original = Replay(labels, baseline=True).run(expected_lock)
                diagnosed = Replay(labels).run(expected_lock)
                self.assertEqual(original, diagnosed)

    def test_no_wrong_candidate_in_search_regions(self):
        # Use all pixels in both real search areas, including auto/decoration.
        for label in ['mumu_A', 'mumu_B', 'desktop_lock']:
            image = frame(label)
            for name, rule in rules().items():
                results = cv2.matchTemplate(rule.corp(image), rule.image, cv2.TM_CCOEFF_NORMED)
                for y, x in np.argwhere(results > rule.threshold):
                    self.assertLessEqual(abs(int(x) + rule.roi_back[0] - 700), 2)
                    self.assertLessEqual(abs(int(y) + rule.roi_back[1] - 658), 2)

    def test_raid_resources_cannot_be_substituted_for_orochi(self):
        raid_specs = specifications('tasks/RealmRaid/assets.py', {'I_LOCK', 'I_UNLOCK'})
        raid_specs.update(specifications('tasks/RyouToppa/assets.py', {'I_TOPPA_LOCK_TEAM', 'I_TOPPA_UNLOCK_TEAM'}))
        for name, values in raid_specs.items():
            rule = RuleImage(**dict(values, file=str(ROOT / values['file']), roi_back=(628, 639, 178, 73)))
            for label in ['mumu_A', 'mumu_B']:
                with self.subTest(template=name, image=label):
                    self.assertFalse(rule.match(frame(label)))
                    self.assertLess(rule.last_match_score, 0.6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
