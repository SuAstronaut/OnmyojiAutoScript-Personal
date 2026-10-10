"""Offline lock diagnosis regression; no OAS logger, Config or Device imports."""
import ast
from datetime import datetime
from functools import cached_property
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]


def source_node(path, class_name, method=None):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    if method:
        node = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == method)
    return node


def load_nodes(nodes, namespace):
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[]))
    exec(compile(module, '<isolated OAS source>', 'exec'), namespace)
    return namespace


IMAGE_NS = load_nodes([source_node('module/atom/image.py', 'RuleImage')], dict(
    cv2=cv2, np=np, fromfile=np.fromfile, uint8=np.uint8, Path=Path,
    cached_property=cached_property, RuleImageMallResourceMixin=object,
    get_current_humanizer=lambda: None, logger=logging.getLogger('offline_image'),
))
RuleImage = IMAGE_NS['RuleImage']

# The original control flow, retained only as a behavior comparison oracle.
BASELINE = '''
def check_lock(self, lock=True):
    logger.info('Check lock: %s', lock)
    if lock:
        while 1:
            self.screenshot()
            if self.appear(self.I_OROCHI_LOCK):
                return True
            if self.appear_then_click(self.I_OROCHI_UNLOCK, interval=1):
                continue
    else:
        while 1:
            self.screenshot()
            if self.appear(self.I_OROCHI_UNLOCK):
                return True
            if self.appear_then_click(self.I_OROCHI_LOCK, interval=1):
                continue
'''


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.items = []

    def emit(self, record):
        self.items.append(record)


class StopLoop(Exception):
    pass


class Harness:
    def __init__(self, states, baseline=False, step=0.3):
        self.states = iter(states)
        self.step = step
        self.clock = SimpleNamespace(now=100.0)
        self.clock.time = lambda: self.clock.now
        self.trace = []
        self.records = Records()
        self.logger = logging.getLogger('offline_orochi')
        self.logger.handlers = [self.records]
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.interval_timer = {}
        self.device = SimpleNamespace(image=None, click=self.click)
        for name, roi in [('LOCK', (613, 643, 293, 73)), ('UNLOCK', (628, 639, 178, 73))]:
            rule = RuleImage((683, 660, 30, 22), roi, 'Template matching', 0.7,
                             str(ROOT / f'tasks/Orochi/o/orochi_{name.lower()}.png'))
            original_match = rule.match

            def tracked_match(image, threshold=None, rule=rule, original_match=original_match):
                self.trace.append(('match', rule.name))
                result = original_match(image, threshold)
                self.trace.append(('match_result', rule.name, result, tuple(rule.roi_front)))
                return result

            rule.match = tracked_match
            setattr(self, f'I_OROCHI_{name}', rule)
        timer_ns = load_nodes([source_node('module/base/timer.py', 'Timer')], {'time': self.clock})
        namespace = dict(logger=self.logger, datetime=datetime, DEBUG=logging.DEBUG,
                         RuleImage=RuleImage, RuleOcr=type('RuleOcr', (), {}),
                         Timer=timer_ns['Timer'])
        nodes = [source_node('tasks/base_task.py', 'BaseTask', n)
                 for n in ('appear', 'appear_then_click')]
        nodes.append(ast.parse(BASELINE).body[0] if baseline else
                     source_node('tasks/Orochi/script_task.py', 'ScriptTask', 'check_lock'))
        load_nodes(nodes, namespace)
        self._appear = namespace['appear'].__get__(self)
        self.appear_then_click = namespace['appear_then_click'].__get__(self)
        self.check_lock = namespace['check_lock'].__get__(self)

    def screenshot(self):
        self.trace.append(('screenshot',))
        try:
            state = next(self.states)
        except StopIteration:
            raise StopLoop('simulated screenshot watchdog')
        self.clock.now += self.step
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        if state != 'blank':
            image[660:682, 683:713] = getattr(self, f'I_OROCHI_{state.upper()}').image
        self.device.image = image

    def appear(self, rule, **kwargs):
        self.trace.append(('appear', rule.name, tuple(sorted(kwargs.items()))))
        return self._appear(rule, **kwargs)

    def click(self, x, y, **kwargs):
        self.trace.append(('click', x, y, tuple(sorted(kwargs.items()))))

    def run(self, lock=True):
        np.random.seed(12345)
        try:
            outcome = self.check_lock(lock)
        except StopLoop:
            outcome = 'watchdog'
        rng = np.random.get_state()
        timers = {name: (timer._current, timer._reach_count, timer.limit)
                  for name, timer in self.interval_timer.items()}
        return outcome, self.trace, timers, rng[1].tolist(), rng[2]

    @property
    def messages(self):
        return [r.getMessage() for r in self.records.items if r.levelno == logging.DEBUG]


class CheckLockDiagnosticsTests(unittest.TestCase):
    def test_behavior_screenshots_match_order_clicks_timers_and_rng_unchanged(self):
        cases = [(['lock'], True), (['unlock', 'lock'], True),
                 (['blank', 'blank', 'lock'], True),
                 (['unlock', 'unlock', 'unlock', 'unlock', 'lock'], True),
                 (['blank'] * 5, True), ([], True),
                 (['unlock'], False), (['lock', 'unlock'], False)]
        for states, lock in cases:
            with self.subTest(states=states, lock=lock):
                old = Harness(states, baseline=True).run(lock)
                new = Harness(states).run(lock)
                self.assertEqual(old, new)

    def test_lock_success_logs_return_and_skipped_unlock(self):
        task = Harness(['lock'])
        task.I_OROCHI_UNLOCK.last_match_score = 0.987654
        self.assertTrue(task.run()[0])
        self.assertIn('LOCK=1.000000 UNLOCK=N/A event=LOCK_success_return', task.messages[-1])
        self.assertIn('loop=1 now=', task.messages[-1])
        self.assertNotIn(('match', 'OROCHI_UNLOCK'), task.trace)
        self.assertTrue(any('event=screenshot_begin' in m for m in task.messages))
        self.assertTrue(any('event=screenshot_ready' in m for m in task.messages))

    def test_unlock_click_and_interval_skip_do_not_reuse_score(self):
        task = Harness(['unlock', 'unlock', 'lock'])
        task.run()
        click = next(m for m in task.messages if 'event=UNLOCK_click_returned' in m)
        self.assertIn('loop=1', click)
        self.assertIn('UNLOCK=1.000000', click)
        skipped = next(m for m in task.messages if 'event=UNLOCK_interval_skip' in m)
        self.assertIn('loop=2', skipped)
        self.assertIn('UNLOCK=N/A', skipped)
        self.assertEqual(sum(e == ('match', 'OROCHI_UNLOCK') for e in task.trace), 1)
        self.assertEqual(sum(e[0] == 'click' for e in task.trace), 1)

    def test_failed_matches_log_actual_scores_and_increasing_loop(self):
        task = Harness(['blank', 'blank', 'lock'])
        task.run()
        failures = [m for m in task.messages if 'event=UNLOCK_failed' in m]
        self.assertEqual(len(failures), 2)
        for index, message in enumerate(failures, 1):
            self.assertIn(f'loop={index}', message)
            self.assertIn('LOCK=0.000000 UNLOCK=0.000000', message)

    def test_debug_visible_at_info_and_logger_level_restored_on_return_or_exception(self):
        for states in (['lock'], [], ['blank']):
            with self.subTest(states=states):
                task = Harness(states)
                child = task.logger.getChild('orochi_lock_diag')
                child.setLevel(logging.WARNING)
                task.run()
                self.assertEqual(task.logger.level, logging.INFO)
                self.assertEqual(child.level, logging.WARNING)
                self.assertTrue(task.messages)
                task.logger.debug('unrelated DEBUG must remain filtered')
                self.assertFalse(any('unrelated DEBUG' in r.getMessage() for r in task.records.items))

    def test_false_branch_has_no_diagnostic_logs(self):
        task = Harness(['lock', 'unlock'])
        self.assertTrue(task.run(False)[0])
        self.assertEqual(task.messages, [])

    def test_score_is_exact_existing_opencv_result_and_threshold_stays_strict(self):
        task = Harness(['blank'])
        task.screenshot()
        rule = task.I_OROCHI_LOCK
        for score in (0.699999, 0.7, 0.700001):
            with self.subTest(score=score), patch.object(cv2, 'minMaxLoc', return_value=(0, score, (0, 0), (70, 17))):
                self.assertEqual(rule.match(task.device.image), score > 0.7)
                self.assertEqual(rule.last_match_score, score)

    def test_guard_rejection_clears_stale_score_without_template_match(self):
        task = Harness(['blank'])
        rule = task.I_OROCHI_LOCK
        rule.last_match_score = 0.99
        rule.guard = SimpleNamespace(match=lambda image: False)
        with patch.object(cv2, 'matchTemplate', side_effect=AssertionError('extra match')):
            self.assertFalse(rule.match(np.zeros((720, 1280, 3), dtype=np.uint8)))
        self.assertIsNone(rule.last_match_score)

    def test_saved_incident_images_keep_existing_scores_results_and_rois(self):
        images = [
            'screenshots/desktop_capture/desktop_client_20261010_222913_207064.png',
            'log/error/1791645548437/2026-10-10_23-19-08-140310.png',
        ]
        for path in images:
            if not (ROOT / path).is_file():
                self.skipTest('Local incident image unavailable: ' + path)
            image = cv2.cvtColor(cv2.imread(str(ROOT / path)), cv2.COLOR_BGR2RGB)
            task = Harness([])
            for rule in (task.I_OROCHI_LOCK, task.I_OROCHI_UNLOCK):
                with self.subTest(image=path, rule=rule.name):
                    initial_roi = tuple(rule.roi_front)
                    result = cv2.matchTemplate(rule.corp(image), rule.image, cv2.TM_CCOEFF_NORMED)
                    _, expected_score, _, location = cv2.minMaxLoc(result)
                    self.assertEqual(rule.match(image), expected_score > rule.threshold)
                    self.assertEqual(rule.last_match_score, expected_score)
                    expected_roi = ((location[0] + rule.roi_back[0], location[1] + rule.roi_back[1],
                                     initial_roi[2], initial_roi[3])
                                    if expected_score > rule.threshold else initial_roi)
                    self.assertEqual(tuple(rule.roi_front), expected_roi)


if __name__ == '__main__':
    unittest.main(verbosity=2)
