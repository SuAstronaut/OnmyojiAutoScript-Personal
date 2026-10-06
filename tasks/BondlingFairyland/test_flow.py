"""Offline BondlingFairyland regression using actual source, timers and click gates.

Only screenshots, OCR and device input are simulated. Never import the OAS app,
logger or Device; pytest runs without touching a game client or user config.
"""
import ast
import copy
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Union

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = 'tasks/BondlingFairyland/script_task.py'
BATTLE = 'tasks/BondlingFairyland/battle.py'


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now


class Log:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(str(message))

    warning = error = hr = info


class TaskEnd(Exception):
    pass


class PageUnknown(Exception):
    pass


class Rule:
    def __init__(self, name):
        self.name = name
        self.roi_front = (0, 0, 1, 1)

    def match(self, image, threshold=None):
        return self.name in image

    def coord(self):
        return 0, 0


class Ocr:
    def __init__(self, name, core):
        self.name, self.core = name, core

    def ocr(self, image):
        self.core.ocr_reads.append(self.name)
        return (25, 74, 99) if self.name == 'O_B_BALL_NUMBER' else (6, 194, 200)


class Core:
    def __init__(self, namespace, frames, mode, step=0.3):
        self.ns = namespace
        self.frames = list(frames)
        self.step = step
        self.clock = namespace['time']
        self.log = namespace['logger']
        self.shots = 0
        self.events = []
        self.ocr_reads = []
        self.interval_timer = {}
        self.settings = []
        self.current_count = 0
        self.limit_count = 2
        self.start_time = datetime.now()
        self.limit_time = timedelta(hours=1)
        self.device = SimpleNamespace(image=set(), click=self.device_click,
                                      stuck_record_add=lambda _: None,
                                      click_record_clear=lambda: None)
        config = SimpleNamespace(bondling_mode=mode, user_status=namespace['UserStatus'].ALONE,
                                 bondling_stone_class=namespace['BondlingClass'].BONDLING_2_2,
                                 bondling_stone_enable=False, bondling_search_enable=False,
                                 limit_count=2)
        self.config = SimpleNamespace(bondling_fairyland=SimpleNamespace(
            bondling_config=config, battle_config=SimpleNamespace(random_click_swipt_enable=False)))
        self.catch_results = []
        self.navigation_ok = True
        self.main_confirmed = True
        self.ui_current = None

    def __getattr__(self, name):
        if name.startswith(('I_', 'C_')):
            rule = Rule(name)
        elif name.startswith('O_'):
            rule = Ocr(name, self)
        else:
            raise AttributeError(name)
        setattr(self, name, rule)
        return rule

    def screenshot(self):
        self.shots += 1
        assert self.shots < 300, 'Offline guard: an unbounded loop was entered'
        self.clock.now += self.step
        if self.frames:
            self.device.image = set(self.frames.pop(0))
        return self.device.image

    def device_click(self, x, y, **kwargs):
        self.events.append(('click', kwargs['control_name']))

    def reward_click_actions(self):
        self._reward_grid_found = 'GRID' in self.device.image
        return [self.C_REWARD_1]

    def settlement_gesture(self, action, control_name='Reward'):
        self.events.append(('settlement', control_name))
        self._settlement_cd_touch()

    def random_click_swipt(self):
        self.events.append(('random',))

    def ui_get_current_page(self):
        self.events.append(('ui_get',))
        normal = {'I_BALL_AREA', 'I_BF_STORE', 'I_BALL_FIRE'}
        if not self.device.image.intersection(normal):
            self.events.append(('BACK_MAIN',))
        self.ui_current = 'main'
        return self.ui_current

    def ui_goto(self, page):
        self.events.append(('goto', page))
        if self.navigation_ok:
            self.ui_current = page
            if page == 'bondling':
                self.frames = [SEARCH, SEARCH]
        return self.navigation_ok

    def ui_wait_until_appear(self, page, **kwargs):
        self.events.append(('confirm', page, kwargs['timeout']))
        return self.main_confirmed and self.ui_current == page

    def set_next_run(self, **kwargs):
        self.events.append(('next_run', kwargs))

    def ball_click(self, index):
        self.events.append(('ball', index))
        return True

    def run_stone(self, enabled):
        return False

    def lock_team(self):
        pass

    def run_alone(self):
        self.events.append(('start_battle',))

    def run_battle(self, *args, **kwargs):
        self.current_count += 1
        return self.catch_battle_wait(False)

    def ui_click(self, click, stop, **kwargs):
        self.settings.append((click.name, stop.name))

    def ui_click_until_disappear(self, target, **kwargs):
        self.settings.append((target.name,))


def load_class(namespace, path, class_name, name, base, methods, source=None):
    tree = ast.parse(source or (ROOT / path).read_text(encoding='utf-8-sig'))
    node = copy.deepcopy(next(n for n in tree.body
                              if isinstance(n, ast.ClassDef) and n.name == class_name))
    node.name = name
    node.bases = [ast.Name(id=base, ctx=ast.Load())] if base else []
    node.keywords = []
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    for function in node.body:
        function.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(ROOT / path), 'exec'), namespace)
    return namespace[name]


ENUM_NS = {'Enum': Enum}
enum_tree = ast.parse((ROOT / 'tasks/BondlingFairyland/config.py').read_text(encoding='utf-8-sig'))
enum_nodes = [n for n in enum_tree.body if isinstance(n, ast.ClassDef)
              and n.name in {'BondlingMode', 'UserStatus', 'BondlingClass'}]
exec(compile(ast.Module(body=enum_nodes, type_ignores=[]), 'bondling_enums', 'exec'), ENUM_NS)
Mode = ENUM_NS['BondlingMode']
SEARCH = {'I_BF_STORE'}
CATCH = {'I_BALL_FIRE'}
AREA = {'I_BALL_AREA'}


@pytest.fixture(scope='module')
def baseline_battle():
    # Read-only comparison with the untouched pre-change implementation.
    return subprocess.run(['git', 'show', 'HEAD:' + BATTLE], cwd=ROOT,
                          check=True, capture_output=True, encoding='utf-8').stdout


def runtime(frames, mode=Mode.MODE3, step=0.3, baseline=None):
    ns = dict(ENUM_NS, Core=Core, time=Clock(), logger=Log(), Union=Union,
              datetime=datetime, timedelta=timedelta, BondlingConfig=object, BattleConfig=object,
              TaskEnd=TaskEnd, GamePageUnknownError=PageUnknown,
              BondlingNumberMax=type('BondlingNumberMax', (Exception,), {}),
              page_main='main', page_bondling_fairyland='bondling',
              RuleImage=Rule, RuleGif=Rule, RuleClick=Rule,
              RuleOcr=Ocr, RuleLongClick=type('LongClick', (), {}),
              weighted_choice=lambda rules: rules[0])
    timer_tree = ast.parse((ROOT / 'module/base/timer.py').read_text(encoding='utf-8-sig'))
    timer = next(n for n in timer_tree.body if isinstance(n, ast.ClassDef) and n.name == 'Timer')
    exec(compile(ast.Module(body=[timer], type_ignores=[]), 'actual_Timer', 'exec'), ns)
    load_class(ns, 'tasks/base_task.py', 'BaseTask', 'Base', 'Core', {'appear', 'appear_then_click'})
    load_class(ns, 'tasks/Component/GeneralBattle/general_battle.py', 'GeneralBattle', 'General', 'Base',
               {'settlement_click', 'settlement_click_grid', '_settlement_cd_ready',
                '_settlement_cd_touch', 'win_appear', 'reward_grid_appear'})
    reward_tree = ast.parse((ROOT / 'tasks/Component/GeneralBattle/reward_frame.py').read_text('utf-8-sig'))
    cooldown = next(n for n in reward_tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'SETTLEMENT_CLICK_CD_S' for t in n.targets))
    ns['SETTLEMENT_CLICK_CD_S'] = ast.literal_eval(cooldown.value)
    load_class(ns, BATTLE, 'BondlingBattle', 'Battle', 'General',
               {'catch_battle_wait', '_clear_catch_settlement'}, source=baseline)
    task_cls = load_class(ns, SCRIPT, 'ScriptTask', 'Task', 'Battle',
                          {'switch_ball', 'run_catch', 'capture_setting', '_wait_bondling_page',
                           'in_search_ui', 'in_catch_ui'})
    return task_cls(ns, frames, mode, step)


def outer_runtime(frames, results, **kwargs):
    task = runtime(frames, **kwargs)
    task.catch_results = list(results)

    def run_catch(*args):
        task.events.append(('run_catch',))
        assert task.catch_results, 'Unexpected extra round'
        return task.catch_results.pop(0)

    task.run_catch = run_catch
    return task


@pytest.mark.parametrize('normal,state', [(AREA, 'search'), (SEARCH, 'search'), (CATCH, 'catch')])
def test_two_fresh_normal_frames_continue_without_navigation(normal, state):
    task = runtime([normal, normal])
    assert task._wait_bondling_page() == state
    assert task.shots == 2 and task.events == []


@pytest.mark.parametrize('normal', [AREA, SEARCH, CATCH])
@pytest.mark.parametrize('unknown_frames', [1, 6])
def test_transient_unknown_or_two_second_animation_recovers_locally(normal, unknown_frames):
    task = runtime([set()] * unknown_frames + [normal, normal])
    assert task._wait_bondling_page() is not None
    assert task.shots == unknown_frames + 2 and task.events == []
    assert task.clock.now < 103


def test_different_normal_states_and_unknown_reset_consecutive_confirmation():
    task = runtime([SEARCH, CATCH, set(), AREA, AREA])
    assert task._wait_bondling_page() == 'search'
    assert task.shots == 5 and task.events == []


def test_continuous_unknown_has_one_total_deadline_and_no_guess_click():
    task = runtime([set()])
    assert task._wait_bondling_page() is None
    assert 3 <= task.clock.now - 100 < 3.4
    assert task.events == []


def test_a_slow_screenshot_after_deadline_does_not_confirm_a_page():
    task = runtime([SEARCH, SEARCH], step=1.6)
    assert task._wait_bondling_page() is None
    assert task.shots == 2 and task.events == []


@pytest.mark.parametrize('overlay', ['I_REWARD', 'GRID', 'I_WIN', 'I_WIN_2',
                                   'I_CAP_SUCCESS', 'I_CAP_FAILURE', 'I_CAP_AGAIN',
                                   'I_BATTLE_SUCCESS', 'I_BATTLE_FAIL', 'I_BATTLE_FAIL_ABANDON'])
def test_result_and_reward_overlay_never_count_as_ready_even_with_background(overlay):
    task = runtime([AREA | CATCH | {overlay}, AREA | CATCH | {overlay}, SEARCH, SEARCH])
    assert task._wait_bondling_page() == 'search'
    assert task.shots == 4
    assert ('start_battle',) not in task.events
    assert ('click', 'I_CAP_AGAIN') not in task.events
    assert not any(e[0] in {'ui_get', 'BACK_MAIN'} for e in task.events)


def test_persistent_reward_uses_safe_click_cooldown_and_keeps_total_deadline():
    task = runtime([SEARCH | {'I_REWARD'}])
    assert task._wait_bondling_page() is None
    assert 3 <= task.clock.now - 100 < 3.4
    assert [e for e in task.events if e[0] == 'settlement'] == [
        ('settlement', 'I_REWARD'), ('settlement', 'I_REWARD')]


def test_reward_during_shared_cooldown_is_still_an_overlay():
    task = runtime([SEARCH | {'I_REWARD'}] * 2 + [SEARCH, SEARCH])
    task._settlement_click_ts = task.clock.now
    assert task._wait_bondling_page() == 'search'
    assert task.shots == 4 and task.events == []


def test_catch_page_after_return_continues_without_reselecting_or_main_recovery():
    task = outer_runtime([SEARCH, SEARCH, set(), CATCH, CATCH, SEARCH, SEARCH], [True, False])
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert task.events.count(('ball', 6)) == 1
    assert task.events.count(('run_catch',)) == 2
    assert ('BACK_MAIN',) not in task.events
    first_run = task.events.index(('run_catch',))
    second_run = task.events.index(('run_catch',), first_run + 1)
    assert not any(e[0] in {'ui_get', 'goto'} for e in task.events[first_run:second_run])
    assert 'Capture loop returned to bondling page; target index: 6' in task.log.messages
    assert not any('Catch successful' in m for m in task.log.messages)


def test_search_page_after_return_reselects_configured_target_without_main_recovery():
    task = outer_runtime([AREA, AREA, set(), SEARCH, SEARCH, SEARCH, SEARCH], [True, False])
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert task.events.count(('ball', 6)) == 2
    assert ('BACK_MAIN',) not in task.events


def test_outer_unknown_timeout_then_existing_recovery_is_allowed():
    task = outer_runtime([SEARCH, SEARCH] + [set()] * 12, [True, False])
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert ('BACK_MAIN',) in task.events
    assert ('goto', 'bondling') in task.events
    assert task.events.index(('BACK_MAIN',)) < task.events.index(('goto', 'bondling'))


def test_inner_unknown_timeout_returns_via_existing_recovery():
    task = runtime([set()])
    assert task.run_catch(task.config.bondling_fairyland.bondling_config, None) is True
    assert ('ui_get',) in task.events and ('goto', 'bondling') in task.events
    assert 3 <= task.clock.now - 100 < 4
    assert ('start_battle',) not in task.events


def test_task_finish_on_unknown_page_is_bounded_then_confirms_main():
    task = outer_runtime([SEARCH, SEARCH] + [set()] * 20, [False])
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert 3 <= task.clock.now - 100 < 4
    assert ('BACK_MAIN',) in task.events
    assert task.events[-3][0:2] == ('goto', 'main')
    assert task.events[-2] == ('confirm', 'main', 5)
    assert task.events[-1][0] == 'next_run'


def test_task_finish_exits_catch_locally_and_still_confirms_main():
    task = outer_runtime([CATCH, CATCH, CATCH | {'I_BACK_Y'}, SEARCH, SEARCH], [False])
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert ('click', 'I_BACK_Y') in task.events
    assert ('confirm', 'main', 5) in task.events
    assert ('BACK_MAIN',) not in task.events


@pytest.mark.parametrize('failure', ['navigation', 'confirmation'])
def test_failed_main_return_never_reports_task_success(failure):
    task = outer_runtime([SEARCH] * 4, [False])
    task.navigation_ok = failure != 'navigation'
    task.main_confirmed = failure != 'confirmation'
    with pytest.raises(PageUnknown, match='return to page_main'):
        task.switch_ball()
    assert not any(e[0] == 'next_run' for e in task.events)
    assert 'BondlingFairyland task finished' not in task.log.messages


def test_failed_bondling_recovery_does_not_loop_forever():
    task = outer_runtime([set()], [], step=0.3)
    task.navigation_ok = False
    with pytest.raises(PageUnknown, match='page recovery failed'):
        task.switch_ball()
    assert task.events.count(('goto', 'bondling')) == 1


@pytest.mark.parametrize('mode,plate', [(Mode.MODE2, 'LOW'), (Mode.MODE3, 'MIDUM'), (Mode.MODE4, 'HIGH')])
def test_plate_settings_keep_existing_grade_and_low_middle_options(mode, plate):
    task = runtime([set()], mode)
    task.capture_setting(mode)
    assert ('I_C_' + plate + '_FALSE', 'I_C_' + plate + '_TRUE') in task.settings
    assert ('I_C_AUTO_FALSE', 'I_C_AUTO_TRUE') in task.settings
    for option in ('I_C_CONTINUOUS_DISABLE', 'I_C_FIRST_DISABLE'):
        assert ((option,) in task.settings) == (mode in (Mode.MODE2, Mode.MODE3))


def test_search_mode_does_not_change_capture_settings():
    task = runtime([set()], Mode.MODE1)
    task.capture_setting(Mode.MODE1)
    assert task.settings == []


@pytest.mark.parametrize('mode', [Mode.MODE2, Mode.MODE3])
def test_first_failure_abandons_then_rewards_without_any_again_click(mode):
    failure = {'I_CAP_AGAIN', 'I_BATTLE_FAIL_ABANDON'}
    task = runtime([failure, {'I_REWARD'}, {'I_REWARD'}, set()], mode)
    assert task.catch_battle_wait(False) is False
    assert ('click', 'I_BATTLE_FAIL_ABANDON') in task.events
    assert ('click', 'I_CAP_AGAIN') not in task.events
    assert ('settlement', 'I_REWARD') in task.events
    assert 'Catch failure' in task.log.messages and 'Get reward' in task.log.messages


def test_low_and_middle_failure_processing_is_identical():
    frames = [{'I_CAP_FAILURE', 'I_CAP_AGAIN', 'I_BATTLE_FAIL_ABANDON'},
              {'I_REWARD'}, {'I_REWARD'}, set()]
    low, middle = runtime(frames, Mode.MODE2), runtime(frames, Mode.MODE3)
    assert low.catch_battle_wait(False) == middle.catch_battle_wait(False)
    assert low.events == middle.events


@pytest.mark.parametrize('mode', [Mode.MODE2, Mode.MODE3, Mode.MODE4])
def test_first_success_keeps_success_settlement(mode):
    task = runtime([{'I_CAP_SUCCESS'}, {'I_BATTLE_SUCCESS'}, {'I_REWARD'}, {'I_REWARD'}, set()], mode)
    assert task.catch_battle_wait(False) is True
    assert ('click', 'I_CAP_SUCCESS') in task.events
    assert ('click', 'I_CAP_AGAIN') not in task.events
    assert ('settlement', 'I_REWARD') in task.events
    assert 'Catch success' in task.log.messages


def test_unknown_capture_result_is_not_logged_as_failure():
    task = runtime([{'I_REWARD'}, set()])
    assert task.catch_battle_wait(False) is False
    assert 'Catch result unknown' in task.log.messages
    assert 'Catch failure' not in task.log.messages


def test_capture_success_appearing_during_reward_cleanup_updates_actual_result():
    task = runtime([{'I_REWARD'}, {'I_CAP_SUCCESS'}, {'I_REWARD'}, set()])
    assert task.catch_battle_wait(False) is True
    assert 'Catch success' in task.log.messages
    assert 'Catch result unknown' not in task.log.messages


def test_high_mode_keeps_retry_and_ten_retry_limit():
    task = runtime([{'I_CAP_AGAIN'}] * 12 + [{'I_BATTLE_FAIL_ABANDON'}, {'I_REWARD'}, set()],
                   Mode.MODE4, step=1.1)
    assert task.catch_battle_wait(False) is False
    assert task.events.count(('click', 'I_CAP_AGAIN')) == 10
    assert ('click', 'I_BATTLE_FAIL_ABANDON') in task.events


@pytest.mark.parametrize('mode', [Mode.MODE1, Mode.MODE2, Mode.MODE4])
def test_unmodified_modes_match_head_battle_behavior(mode, baseline_battle):
    frames = [{'I_CAP_AGAIN', 'I_BATTLE_FAIL_ABANDON'},
              {'I_CAP_SUCCESS'}, {'I_REWARD'}, {'I_REWARD'}, set()]
    before = runtime(frames, mode, baseline=baseline_battle)
    after = runtime(frames, mode)
    assert before.catch_battle_wait(False) == after.catch_battle_wait(False)
    assert before.events == after.events


@pytest.mark.parametrize('mode,ocr', [(Mode.MODE2, 'O_B_LOW_NUMBER'), (Mode.MODE3, 'O_B_MEDIUM_NUMBER')])
def test_inner_battle_continues_locally_and_reads_only_its_plate_inventory(mode, ocr):
    task = runtime([CATCH] * 6, mode)
    task.config.bondling_fairyland.bondling_config.limit_count = 1
    task.limit_count = 1
    # Isolate navigation from combat; battle returns failure to prove it does not force home.
    def battle(*args, **kwargs):
        task.current_count += 1
        return False
    task.run_battle = battle
    assert task.run_catch(task.config.bondling_fairyland.bondling_config, None) is False
    assert task.events == [('start_battle',)]
    assert ocr in task.ocr_reads
    assert not any(n.startswith('O_B_') and n != ocr and n != 'O_B_BALL_NUMBER'
                   for n in task.ocr_reads)


@pytest.mark.parametrize('mode', [Mode.MODE2, Mode.MODE3])
@pytest.mark.parametrize('first_success', [False, True])
def test_two_real_rounds_through_outer_inner_battle_and_final_main(mode, first_success):
    failure = [{'I_CAP_AGAIN', 'I_BATTLE_FAIL_ABANDON'}, {'I_BATTLE_FAIL'},
               {'I_REWARD'}, {'I_REWARD'}, set()]
    first = ([{'I_CAP_SUCCESS'}, {'I_BATTLE_SUCCESS'}, {'I_REWARD'}, {'I_REWARD'}, set()]
             if first_success else failure)
    frames = [SEARCH, SEARCH] + [CATCH] * 5 + first
    if first_success:
        # Captured target returns to the map; a transient missing frame must not go home.
        frames += [AREA, AREA, set(), SEARCH, SEARCH] + [CATCH] * 5
    else:
        frames += [CATCH] * 3  # Same target can continue directly after a failed contract.
    frames += failure + [CATCH] * 3 + [CATCH | {'I_BACK_Y'}, SEARCH, SEARCH]
    task = runtime(frames, mode)
    with pytest.raises(TaskEnd):
        task.switch_ball()
    assert task.current_count == 2
    assert task.events.count(('start_battle',)) == 2
    assert task.events.count(('ball', 6)) == (2 if first_success else 1)
    assert ('click', 'I_CAP_AGAIN') not in task.events
    assert ('BACK_MAIN',) not in task.events
    assert ('goto', 'bondling') not in task.events
    assert task.events.count(('goto', 'main')) == 1
    assert task.events[-2] == ('confirm', 'main', 5)
    assert task.events[-1][0] == 'next_run'
    assert task.log.messages.count('Get reward') == 2
    assert task.log.messages.count('Catch success') == int(first_success)
    assert task.log.messages.count('Catch failure') == (1 if first_success else 2)
