"""Offline regressions for real RealmRaid methods; no app or device imports.

Template hits and screenshots are simulated. This verifies control flow, not
the templates' recognition accuracy on real game pixels.
"""
import ast
import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).with_name('script_task.py')


class StuckError(Exception):
    pass


class EndTask(Exception):
    pass


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class Click:
    def __init__(self, roi_front, roi_back, name):
        self.roi_front = roi_front
        self.name = name

    def coord(self):
        x, y, w, h = self.roi_front
        return x + w // 2, y + h // 2


class Template:
    def __init__(self, name):
        self.name = name
        self.roi_back = (0, 0, 1280, 720)

    def match_all_any(self, image, roi):
        kind = 'M' if self.name == 'MEDAL' else 'N'
        return [(1, x, y, 31, 31) for x, y, tag in image.hits if tag == kind]

    def match(self, image, threshold=None):
        x, y, _, _ = self.roi_back
        col = min(range(3), key=lambda i: abs(x - (145 + 332 * i)))
        row = min(range(3), key=lambda i: abs(y - (144 + 135 * i)))
        index = row * 3 + col + 1
        marks = image.finished if self.name == 'FINISH' else image.failed
        return index in marks


def frame(finished=(), failed=(), missing=(), visible=('PERSON',), medals=None,
          counts=None):
    hits = []
    medals = medals or {}
    counts = counts or {}
    for index in range(1, 10):
        if index in missing:
            continue
        x = 242 + 332 * ((index - 1) % 3)
        y = 211 + 135 * ((index - 1) // 3)
        slots = counts.get(index, 4 if index in finished else 5)
        for slot in range(slots):
            kind = 'M' if slot < medals.get(index, 0) else 'N'
            hits.append((int(x + 39.4 * slot), y + (10 if kind == 'M' else 0), kind))
    return SimpleNamespace(hits=hits, finished=set(finished), failed=set(failed),
                           visible=set(visible))


class Core:
    def __init__(self, frames, clock):
        self.frames = frames
        self.clock = clock
        self.shots = 0
        self.events = []
        self.device = SimpleNamespace(image=frames[0], click=self.device_click)
        self.I_MEDAL = Template('MEDAL')
        self.I_NO_MEDAL = Template('NO_MEDAL')
        self.I_FINISH_SIGN = Template('FINISH')
        self.I_FAILURE_SIGN = Template('FAILURE')
        self.I_RR_PERSON = 'PERSON'
        self.I_FRESH_ENSURE = 'REFRESH_CONFIRM'
        self.I_SOUL_RAID = 'THREE_REWARD'
        self.I_REWARD = 'REWARD'
        self.I_FIRE = 'FIRE'
        self.I_RR_THREE = 'THREE'
        self.I_BACK_RED = 'BACK'
        self.I_CHECK_EXPLORATION = 'EXPLORATION'
        self.attack_by_position = False
        self.attack_order = [5, 4, 3, 2, 1, 0]
        self.current_count = 0
        self.config = SimpleNamespace(realm_raid=SimpleNamespace(
            switch_soul_config=SimpleNamespace(enable=False, enable_switch_by_name=False),
            general_battle_config=SimpleNamespace(lock_team_enable=True),
            raid_config=SimpleNamespace(exit_four=False, auto_exit_all=False,
                number_base=0, number_attack=1, when_attack_fail='CONTINUE',
                three_refresh=False)))
        self.ui_get_current_page = Mock()
        self.ui_goto = Mock()
        self.ensure_lock = Mock()
        self.check_ticket = Mock(return_value=True)
        self.check_refresh = Mock(return_value=False)
        self.reward_detect_click = Mock(return_value=False)
        self.ui_click = Mock()
        self.set_next_run = Mock()
        self.run_general_battle_back = Mock(return_value=True)
        self.run_general_battle = Mock(side_effect=self.battle)

    def screenshot(self):
        if self.shots > 400:
            raise AssertionError('Unexpected unbounded detection loop')
        self.device.image = self.frames[min(self.shots, len(self.frames) - 1)]
        self.shots += 1
        self.clock.now += 0.1

    def _read_level(self, image, x, y):
        return 60

    def appear(self, target):
        return target in self.device.image.visible

    def appear_then_click(self, target, interval=None):
        self.events.append(target)
        return True

    def settlement_click(self, target, action):
        self.events.append(target)

    def device_click(self, x, y, **kwargs):
        self.events.append(kwargs.get('control_name', 'DIRECT'))

    def click(self, click, interval=None):
        self.events.append(click.name)
        return True

    def ui_page_appear(self, page):
        return 'PERSON' in self.device.image.visible

    def is_in_prepare(self, screenshot):
        return 'PREPARE' in self.device.image.visible

    def is_in_real_battle(self, screenshot):
        return 'BATTLE' in self.device.image.visible

    def battle(self, config):
        self.current_count += 1
        return True


def runtime(frames=None):
    clock = Clock()
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    namespace = dict(Core=Core, time=clock, logger=Mock(), RuleClick=Click,
                     GameStuckError=StuckError, TaskEnd=EndTask,
                     datetime=datetime, timedelta=timedelta,
                     WhenAttackFail=SimpleNamespace(CONTINUE='CONTINUE', REFRESH='REFRESH', EXIT='EXIT'),
                     page_realm_raid=object(), page_main=object(), page_shikigami_records=object())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    namespace[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name == '_cluster':
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    selected = {'_match_in_region', '_slot_hits', 'detect_grid', 'detect_cells',
                '_is_attack_candidate', '_detect_attack_cells', '_validated_target_click',
                'find_one', 'partition', 'fire', 'run_2', 'decide_exit_strategy'}
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ScriptTask')
    body = [copy.deepcopy(n) for n in original.body
            if isinstance(n, ast.FunctionDef) and n.name in selected]
    assert len(body) == len(selected)
    for node in body:
        node.returns = None
        for arg in node.args.args + node.args.kwonlyargs:
            arg.annotation = None
    cls = ast.ClassDef(name='Task', bases=[ast.Name(id='Core', ctx=ast.Load())],
                       keywords=[], body=body, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return namespace['Task'](frames or [frame()], clock), clock


class UiRule:
    def __init__(self, name):
        self.name = name

    def match(self, image, threshold=None):
        return self.name in image.visible

    def coord(self):
        return 100, 100


def bind_input_controls(task, clock):
    """Use the actual input methods and Timer, including unsent interval gates."""
    root = SOURCE.parents[2]
    timer_path = root / 'module/base/timer.py'
    timer_tree = ast.parse(timer_path.read_text(encoding='utf-8-sig'))
    timer = next(n for n in timer_tree.body if isinstance(n, ast.ClassDef) and n.name == 'Timer')
    namespace = dict(time=SimpleNamespace(time=lambda: 100 + clock.now),
                     RuleImage=UiRule, RuleGif=UiRule, RuleClick=Click,
                     RuleOcr=type('Ocr', (), {}), RuleLongClick=type('LongClick', (), {}))
    exec(compile(ast.Module(body=[timer], type_ignores=[]), str(timer_path), 'exec'), namespace)
    base_path = root / 'tasks/base_task.py'
    base = next(n for n in ast.parse(base_path.read_text(encoding='utf-8-sig')).body
                if isinstance(n, ast.ClassDef) and n.name == 'BaseTask')
    methods = [copy.deepcopy(n) for n in base.body if isinstance(n, ast.FunctionDef)
               and n.name in ('appear', 'appear_then_click', 'click')]
    for node in methods:
        node.returns = None
        for arg in node.args.args + node.args.kwonlyargs:
            arg.annotation = None
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 str(base_path), 'exec'), namespace)
    task.interval_timer = {}
    task.input_timer_class = namespace['Timer']
    for name, label in [('I_RR_PERSON', 'PERSON'), ('I_FRESH_ENSURE', 'REFRESH_CONFIRM'),
                        ('I_SOUL_RAID', 'THREE_REWARD'), ('I_REWARD', 'REWARD'), ('I_FIRE', 'FIRE')]:
        setattr(task, name, UiRule(label))
    for name in ('appear', 'appear_then_click', 'click'):
        setattr(task, name, namespace[name].__get__(task))
    return task


def fire_scenario(mode='slow', delay=0.5, returned_state='normal', retreat=False):
    """UI responses depend on actual sent inputs, not on mock success values."""
    task, clock = runtime()
    bind_input_controls(task, clock)
    state = dict(target_times=[], fire_times=[], accepted=False, rejected=False,
                 timer_seeded=False, unsent=0, returned_frames=0)
    real_appear_then_click = task.appear_then_click

    def screenshot():
        clock.now += 0.1
        if state['accepted']:
            visible = ('PREPARE',)
        elif not state['target_times']:
            visible = ('PERSON',)
        elif mode == 'slow':
            visible = ('FIRE',) if clock.now >= state['target_times'][0] + delay else ('PERSON',)
        elif mode == 'no_response':
            visible = ('PERSON',)
        elif mode in ('persistent_fire', 'slow_prepare'):
            visible = ('FIRE',)
            if mode == 'slow_prepare' and state['fire_times'] and clock.now >= state['fire_times'][0] + delay:
                visible = ('PREPARE',)
        elif state['rejected']:
            state['returned_frames'] += 1
            visible = () if mode == 'alternating_list' and state['returned_frames'] % 2 == 0 else ('PERSON',)
        else:
            visible = ('FIRE',) if clock.now >= state['target_times'][-1] + 0.3 else ('PERSON',)
        if mode == 'unsent' and visible == ('FIRE',) and not state['timer_seeded']:
            task.interval_timer['FIRE'] = task.input_timer_class(1).reset()
            state['timer_seeded'] = True
        changed = state['rejected'] and returned_state
        task.device.image = frame(visible=visible,
                                  finished=(1,) if changed == 'finished' else (),
                                  failed=(1,) if retreat or changed == 'failed' else (),
                                  missing=(1,) if changed == 'zero' else (1, 2, 3) if changed == 'partial' else ())

    def device_click(x, y, control_name='', **kwargs):
        task.events.append(control_name)
        if control_name.startswith('partition_'):
            state['target_times'].append(clock.now)
            state['rejected'] = False
        elif control_name == 'FIRE':
            state['fire_times'].append(clock.now)
            if mode in ('persistent_fire', 'slow_prepare'):
                return
            if mode in ('sent', 'repeat_return', 'alternating_list') and (
                    len(state['target_times']) == 1 or mode == 'repeat_return'):
                state['rejected'] = True
            else:
                state['accepted'] = True

    def appear_then_click(target, interval=None):
        sent = real_appear_then_click(target, interval=interval)
        if target is task.I_FIRE and mode == 'unsent' and not sent:
            state['unsent'] += 1
            state['rejected'] = True
        return sent

    task.screenshot = screenshot
    task.device.click = device_click
    task.appear_then_click = appear_then_click
    return task, clock, state


class CellStateTests(unittest.TestCase):
    def test_complete_empty_medal_slots_remain_attackable(self):
        task, _ = runtime()
        cells = task.detect_cells(False, read_level=False)
        self.assertEqual(len(cells), 9)
        self.assertEqual((cells[0]['state'], cells[0]['slot_count'], cells[0]['medal_count']),
                         ('ATTACKABLE', 5, 0))
        self.assertEqual(task.find_one(False, cells), 1)

    def test_zero_slots_cannot_win_zero_medal_priority(self):
        task, _ = runtime([frame(missing=(1, 8))])
        cells = task.detect_cells(False, read_level=False)
        self.assertEqual((cells[0]['state'], cells[0]['slot_count']), ('ATTACKABLE', 0))
        self.assertEqual(task.find_one(False, cells), 2)
        self.assertEqual(task.events, [])

    def test_partial_slots_remain_candidates_without_global_retry(self):
        for count in (3, 4):
            with self.subTest(count=count):
                task, clock = runtime([frame(counts={5: count}, medals={5: 2})])
                cells = task._detect_attack_cells()
                self.assertEqual(cells[4]['slot_count'], count)
                self.assertEqual(task.find_one(False, cells), 5)
                self.assertEqual(clock.waits, [])

    def test_zero_cell_does_not_block_other_trusted_targets(self):
        task, clock = runtime([frame(missing=(1,), counts={5: 3})])
        cells = task._detect_attack_cells()
        self.assertEqual(task.find_one(False, cells), 2)
        self.assertEqual(clock.waits, [])

    def test_finished_and_failed_skip_with_original_priority(self):
        task, _ = runtime([frame(finished=(1,), failed=(2,), medals={3: 2, 4: 5})])
        cells = task.detect_cells(False, read_level=False)
        self.assertEqual((cells[0]['state'], cells[0]['slot_count']), ('FINISHED', 4))
        self.assertEqual((cells[1]['state'], cells[1]['slot_count']), ('FAILED', 5))
        self.assertEqual(task.find_one(False, cells), 4)
        task.attack_by_position = True
        self.assertEqual(task.find_one(False, cells), 3)

    def test_finished_sign_wins_even_with_five_medals_and_failure_sign(self):
        task, _ = runtime([frame(finished=(1,), failed=(1,),
                               counts={1: 5}, medals={1: 5})])
        cells = task._detect_attack_cells()
        self.assertEqual((cells[0]['state'], cells[0]['slot_count']), ('FINISHED', 5))
        self.assertEqual(task.find_one(False, cells), 2)

    def test_abnormal_frame_is_rescanned_before_selection(self):
        task, clock = runtime([frame(finished=range(2, 10), missing=(1,)),
                               frame(finished=range(1, 10))])
        task.screenshot()
        cells = task._detect_attack_cells()
        self.assertEqual(cells[0]['state'], 'FINISHED')
        self.assertIsNone(task.find_one(False, cells))
        self.assertEqual(clock.waits, [0.5])
        self.assertEqual(task.events, [])

    def test_persistent_anomaly_is_bounded_and_never_refreshes(self):
        task, clock = runtime([frame(finished=range(2, 10), missing=(1,))])
        with self.assertRaises(StuckError):
            task._detect_attack_cells()
        self.assertEqual(clock.waits, [0.5, 0.5])
        self.assertEqual(task.shots, 2)
        self.assertEqual(task.events, [])
        task.check_refresh.assert_not_called()

    def test_last_zero_cell_recovers_to_partial_trusted_target(self):
        task, clock = runtime([frame(finished=range(2, 10), missing=(1,)),
                               frame(finished=range(2, 10), counts={1: 3})])
        task.screenshot()
        cells = task._detect_attack_cells()
        self.assertEqual(task.find_one(False, cells), 1)
        self.assertEqual(clock.waits, [0.5])

    def test_no_slots_fallback_is_not_accepted(self):
        task, _ = runtime([frame(missing=range(1, 10))])
        with self.assertRaises(StuckError):
            task._detect_attack_cells()
        self.assertEqual(task.events, [])

    def test_partial_grid_is_retried(self):
        task, clock = runtime([frame(missing=(1, 2, 3))])
        with self.assertRaises(StuckError):
            task._detect_attack_cells()
        self.assertEqual(clock.waits, [0.5, 0.5])


class FireValidationTests(unittest.TestCase):
    def test_selected_target_becomes_finished_before_fire(self):
        task, _ = runtime([frame(), frame(finished=(1,))])
        self.assertEqual(task.find_one(False), 1)
        self.assertIs(task.fire(1, revalidate=True), False)
        self.assertEqual(task.events, [])

    def test_target_is_not_reclicked_after_finished_on_retry(self):
        task, _ = runtime([frame(), frame(), frame(), frame(finished=(1,))])
        self.assertIs(task.fire(1, revalidate=True), False)
        self.assertEqual(task.events, ['partition_1'])

    def test_zero_slots_and_failed_cancel_ordinary_fire(self):
        for changed in (frame(missing=(1,)), frame(failed=(1,))):
            with self.subTest(changed=changed):
                task, _ = runtime([frame(), changed])
                self.assertIs(task.fire(1, revalidate=True), False)
                self.assertEqual(task.events, [])

    def test_full_normal_target_enters_battle(self):
        task, _ = runtime([frame(), frame(), frame(),
                           frame(visible=('FIRE',)), frame(visible=('PREPARE',))])
        task.fire(1, revalidate=True)
        self.assertEqual(task.events, ['partition_1', 'FIRE'])

    def test_partial_normal_targets_enter_battle(self):
        for count in (3, 4):
            with self.subTest(count=count):
                normal = frame(counts={1: count})
                task, _ = runtime([normal, normal, frame(visible=('FIRE',)),
                                   frame(visible=('PREPARE',))])
                task.fire(1, revalidate=True)
                self.assertEqual(task.events, ['partition_1', 'FIRE'])

    def test_detail_animation_does_not_trigger_background_validation(self):
        task, _ = runtime([frame(), frame(),
                           frame(missing=(1,), visible=('PERSON',)),
                           frame(missing=(1,), visible=('PERSON', 'FIRE')),
                           frame(missing=(1,), visible=('PERSON',)),
                           frame(visible=('PREPARE',))])
        task.detect_cells = Mock(wraps=task.detect_cells)
        task.fire(1, revalidate=True)
        self.assertEqual(task.events, ['partition_1', 'FIRE'])
        self.assertEqual(task.detect_cells.call_count, 1)

    def test_no_fire_rechecks_target_and_limits_partition_attempts(self):
        task, clock = runtime([frame()])
        task.detect_cells = Mock(wraps=task.detect_cells)
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(1, revalidate=True)
        self.assertEqual(task.events, ['partition_1', 'partition_1'])
        self.assertEqual(task.detect_cells.call_count, 2)
        self.assertGreaterEqual(clock.now, 30)
        self.assertLess(clock.now, 30.5)

    def test_no_fire_rechecks_updated_click_roi_before_second_attempt(self):
        task, _ = runtime([frame()])
        cells = task.detect_cells(False, read_level=False)
        changed = [dict(cell) for cell in cells]
        changed[0]['click_roi'] = (300, 150, 80, 60)
        task.detect_cells = Mock(side_effect=[cells, changed, changed])
        clicked = []
        task.click = lambda target, interval=None: clicked.append(target.roi_front) or True
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(1, revalidate=True)
        self.assertEqual(clicked, [cells[0]['click_roi'], (300, 150, 80, 60)])

    def test_no_fire_then_failed_cancels_ordinary_target(self):
        task, _ = runtime([frame(), frame(), frame(failed=(1,))])
        self.assertIs(task.fire(1, revalidate=True), False)
        self.assertEqual(task.events, ['partition_1'])

    def test_cleanup_then_finished_cancels_old_order(self):
        task, _ = runtime([frame(visible=('PERSON', 'THREE_REWARD')),
                           frame(), frame(finished=(1,))])
        self.assertIs(task.fire(1, revalidate=True), False)
        self.assertEqual(task.events, ['THREE_REWARD'])

    def test_explicit_retreat_reentry_allows_failed(self):
        failed = frame(failed=(9,))
        task, _ = runtime([failed, failed, failed,
                           frame(failed=(9,), visible=('FIRE',)),
                           frame(failed=(9,), visible=('PREPARE',))])
        self.assertEqual(task.detect_cells(False, read_level=False)[8]['state'], 'FAILED')
        task.fire(9)
        self.assertEqual(task.events, ['partition_9', 'FIRE'])

    def test_retreat_retry_accepts_failed_even_without_slots(self):
        failed = frame(failed=(9,), missing=(9,))
        task, _ = runtime([failed])
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(9)
        self.assertEqual(task.events, ['partition_9', 'partition_9'])


class FireRecoveryTests(unittest.TestCase):
    def assert_slow_details(self, delay):
        task, clock, state = fire_scenario(delay=delay)
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertTrue(state['accepted'])
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(len(state['fire_times']), 1)
        self.assertGreaterEqual(clock.now, delay)
        self.assertLess(clock.now, delay + 1)

    def test_six_second_details_use_remaining_deadline(self):
        self.assert_slow_details(6)

    def test_eight_second_details_use_remaining_deadline(self):
        self.assert_slow_details(8)

    def test_permanent_no_response_waits_to_total_deadline(self):
        task, clock, state = fire_scenario('no_response')
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(1, revalidate=True)
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(state['fire_times'], [])
        self.assertGreaterEqual(clock.now, 30)
        self.assertLess(clock.now, 30.5)

    def test_real_timer_unsent_fire_can_reenter_target(self):
        task, clock, state = fire_scenario('unsent')
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertEqual(state['unsent'], 1)
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(len(state['fire_times']), 1)
        self.assertTrue(state['accepted'])

    def test_sent_fire_return_to_attackable_list_can_recover(self):
        task, clock, state = fire_scenario('sent')
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(len(state['fire_times']), 2)
        self.assertTrue(state['accepted'])

    def test_returned_finished_target_cancels_without_reentry(self):
        task, clock, state = fire_scenario('sent', returned_state='finished')
        self.assertIs(task.fire(1, revalidate=True), False)
        self.assertEqual(len(state['target_times']), 1)
        self.assertEqual(len(state['fire_times']), 1)

    def test_returned_untrusted_target_never_reenters(self):
        for changed in ('zero', 'failed', 'partial'):
            with self.subTest(changed=changed):
                task, clock, state = fire_scenario('sent', returned_state=changed)
                self.assertIs(task.fire(1, revalidate=True), False)
                self.assertEqual(len(state['target_times']), 1)
                self.assertEqual(len(state['fire_times']), 1)

    def test_repeated_return_is_bounded_across_entire_fire(self):
        task, clock, state = fire_scenario('repeat_return')
        with self.assertRaisesRegex(StuckError, 'list recovery budget exhausted'):
            task.fire(1, revalidate=True)
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(len(state['fire_times']), 2)
        self.assertLess(clock.now, 30)

    def test_fast_normal_path_has_no_extra_wait(self):
        task, clock, state = fire_scenario(delay=0.5)
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertEqual(len(state['target_times']), 1)
        self.assertEqual(len(state['fire_times']), 1)
        self.assertLess(clock.now, 1)

    def test_persistent_fire_input_is_limited_and_times_out(self):
        task, clock, state = fire_scenario('persistent_fire')
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(1, revalidate=True)
        self.assertEqual(len(state['target_times']), 1)
        self.assertEqual(len(state['fire_times']), 2)
        self.assertGreater(state['fire_times'][1] - state['fire_times'][0], 1)
        self.assertGreaterEqual(clock.now, 30)
        self.assertLess(clock.now, 30.5)

    def test_slow_prepare_does_not_spend_extra_fire_inputs(self):
        task, clock, state = fire_scenario('slow_prepare', delay=6)
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertEqual(len(state['target_times']), 1)
        self.assertEqual(len(state['fire_times']), 2)
        self.assertGreaterEqual(clock.now, 6)

    def test_nonconsecutive_list_frames_do_not_trigger_reentry(self):
        task, clock, state = fire_scenario('alternating_list')
        with self.assertRaisesRegex(StuckError, 'attack timed out'):
            task.fire(1, revalidate=True)
        self.assertEqual(len(state['target_times']), 1)
        self.assertEqual(len(state['fire_times']), 1)
        self.assertGreaterEqual(clock.now, 30)

    def test_retreat_failed_reentry_still_recovers(self):
        task, clock, state = fire_scenario('sent', retreat=True)
        self.assertIsNone(task.fire(1))
        self.assertEqual(len(state['target_times']), 2)
        self.assertEqual(len(state['fire_times']), 2)

    def test_one_bad_target_frame_does_not_cancel_recovery(self):
        task, clock, state = fire_scenario('sent')
        detect = task.detect_cells
        recovery_scans = []
        def transient_detection(*args, **kwargs):
            cells = detect(*args, **kwargs)
            if state['rejected']:
                recovery_scans.append(clock.now)
                if len(recovery_scans) == 1:
                    cells[0]['slot_count'] = 0
            return cells
        task.detect_cells = transient_detection
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertGreaterEqual(len(recovery_scans), 3)
        self.assertEqual(len(state['target_times']), 2)
        self.assertTrue(state['accepted'])

    def test_recovery_requires_same_roi_on_consecutive_frames(self):
        task, clock, state = fire_scenario('sent')
        detect = task.detect_cells
        recovery_scans = []
        def moving_roi(*args, **kwargs):
            cells = detect(*args, **kwargs)
            if state['rejected']:
                recovery_scans.append(clock.now)
                if len(recovery_scans) == 1:
                    cells[0]['click_roi'] = (400, 150, 80, 60)
            return cells
        task.detect_cells = moving_roi
        self.assertIsNone(task.fire(1, revalidate=True))
        self.assertGreaterEqual(len(recovery_scans), 3)
        self.assertEqual(len(state['target_times']), 2)
        self.assertTrue(state['accepted'])


class TaskFlowTests(unittest.TestCase):
    def test_exit_four_keeps_four_retreats_and_failed_reentry(self):
        task, _ = runtime([frame(finished=range(1, 9))])
        task.config.realm_raid.raid_config.exit_four = True
        task.fire = Mock()
        with self.assertRaises(EndTask):
            task.run_2()
        self.assertEqual(task.fire.call_args_list[0].args, (9,))
        self.assertEqual(task.fire.call_args_list[0].kwargs, {'revalidate': True})
        self.assertEqual(len(task.fire.call_args_list), 5)
        self.assertEqual([call.kwargs for call in task.fire.call_args_list[1:]],
                         [{}, {}, {}, {'revalidate': False}])
        self.assertEqual(task.run_general_battle_back.call_count, 4)
        self.assertTrue(all(call.kwargs == {'exit_four': True}
                            for call in task.run_general_battle_back.call_args_list))
        task.run_general_battle.assert_called_once()

    def test_cancelled_target_reselects_without_battle_or_refresh(self):
        task, _ = runtime()
        first = task.detect_cells(False, read_level=False)
        task.device.image = frame(finished=(1,))
        second = task.detect_cells(False, read_level=False)
        task._detect_attack_cells = Mock(side_effect=[first, second])
        task.fire = Mock(side_effect=[False, None])
        with self.assertRaises(EndTask):
            task.run_2()
        self.assertEqual([call.args for call in task.fire.call_args_list], [(1,), (2,)])
        task.run_general_battle.assert_called_once()
        task.check_refresh.assert_not_called()
        task.run_general_battle_back.assert_not_called()

    def test_auto_exit_all_keeps_original_level_strategy(self):
        task, _ = runtime()
        task.config.realm_raid.raid_config.auto_exit_all = True
        task.fire = Mock()
        task.run_general_battle_back.side_effect = lambda *a, **kw: task.battle(None)
        with self.assertRaises(EndTask):
            task.run_2()
        task.fire.assert_called_once_with(1, revalidate=True)
        task.run_general_battle_back.assert_called_once()
        task.run_general_battle.assert_not_called()

    def test_valid_no_candidates_still_uses_refresh_branch(self):
        task, _ = runtime([frame(finished=range(1, 10))])
        task.fire = Mock()
        with self.assertRaises(EndTask):
            task.run_2()
        task.check_refresh.assert_called_once()
        task.fire.assert_not_called()

    def test_last_unknown_cell_does_not_refresh_or_finish(self):
        task, _ = runtime([frame(finished=range(2, 10), missing=(1,))])
        task.fire = Mock()
        with self.assertRaises(StuckError):
            task.run_2()
        task.check_refresh.assert_not_called()
        task.set_next_run.assert_not_called()
        task.fire.assert_not_called()

    def test_three_slot_cell_does_not_stop_normal_task(self):
        task, _ = runtime([frame(counts={5: 3}, medals={5: 2})])
        task.fire = Mock()
        with self.assertRaises(EndTask):
            task.run_2()
        task.fire.assert_called_once_with(5, revalidate=True)
        task.run_general_battle.assert_called_once()

    def test_nine_targets_finish_with_stable_three_slot_match(self):
        task, _ = runtime([frame(counts={5: 3})])
        task.config.realm_raid.raid_config.number_attack = 9
        task.fire = Mock()
        finished = []
        def win(config):
            finished.append(task.fire.call_args.args[0])
            task.current_count += 1
            task.frames = [frame(finished=finished, counts={5: 3})]
            return True
        task.run_general_battle.side_effect = win
        with self.assertRaises(EndTask):
            task.run_2()
        self.assertEqual(finished, list(range(1, 10)))
        self.assertEqual(task.run_general_battle.call_count, 9)
        task.check_refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
