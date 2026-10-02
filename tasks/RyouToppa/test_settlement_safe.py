"""Offline sampling and actual-method regression; never import the OAS application.

Run directly with Python. RAID_VERIFY_OUT optionally writes per-task artifacts.
"""
import ast
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('ryou_safe', HERE / 'settlement_safe.py')
safe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe)


def safe_for_task(task_name):
    if task_name == 'RyouToppa':
        return safe
    from tasks.RealmRaid import settlement_safe
    return settlement_safe


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class Log:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    error = info


class Rule:
    def __init__(self, roi_front=(0, 0, 1, 1), roi_back=None, name='rule'):
        self.roi_front = roi_front
        self.name = name

    def match(self, image, threshold=None):
        return self.name in image.visible

    def coord(self):
        return 10, 10  # Distinguishable from a settlement-safe coordinate.


class Core:
    def __init__(self, clock, frames=()):
        self.clock = clock
        self.frames = list(frames)
        self.shots = 0
        self.events = []
        self.click_shots = []
        self._realm_settlement_active = False
        self._ryou_settlement_active = False
        self.interval_timer = {}
        self.n = 3
        self.device = SimpleNamespace(
            image=SimpleNamespace(shape=(720, 1280, 3), visible=set()),
            click=self.device_click, stuck_record_add=lambda _: None,
            click_record_clear=lambda: None)
        for name in ('I_WIN', 'I_WIN_2', 'I_DE_WIN', 'I_FALSE', 'I_REWARD',
                     'I_REWARD_GOLD', 'I_EXTRA_INFO', 'I_STATISTICS',
                     'I_PREPARE_HIGHLIGHT', 'I_TOPPA_RECORD', 'I_FIRE',
                     'I_CONFIRM_CLOSE_DIFF_SOUL', 'I_EXIT', 'I_EXIT_ENSURE',
                     'I_AGAIN_BATTLE', 'C_RANDOM_CLICK'):
            setattr(self, name, Rule(name=name))

    def device_click(self, x, y, **kwargs):
        self.events.append((x, y, kwargs))
        self.click_shots.append(self.shots)
        self.clock.sleep(0.01)

    def screenshot(self):
        if self.shots > 30:
            raise AssertionError('State machine did not terminate')
        self.clock.sleep(1.1)
        self.device.image.visible = set(self.frames[min(self.shots, len(self.frames)-1)])
        self.shots += 1

    def reward_click_actions(self):
        self._reward_grid_found = 'GRID' in self.device.image.visible
        return [Rule(name='unused_shared_density')]

    def settlement_click_count(self, page_clicks):
        return self.n

    def reward_avoid(self):
        return ()

    def _settlement_point(self, action):
        return 10, 10, Rule(roi_front=(0,0,1280,720))

    def wait_until_appear(self, target, wait_time=None):
        self.wait_budget = wait_time
        return False

    def ui_click_until_disappear(self, target):
        while self.appear(target):
            self.appear_then_click(target, interval=1)
            self.screenshot()


def source_class(path, source_name, output_name, base, methods, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                    and n.name == source_name)
    body = [copy.deepcopy(n) for n in original.body
            if isinstance(n, ast.FunctionDef) and n.name in methods]
    assert len(body) == len(methods), (path, methods)
    for node in body:
        node.returns = None
        for arg in node.args.args + node.args.kwonlyargs:
            arg.annotation = None
    cls = ast.ClassDef(name=output_name, bases=[ast.Name(id=base, ctx=ast.Load())],
                       keywords=[], body=body, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace[output_name]


def runtime(frames=(), task_name='RyouToppa'):
    clock, log = Clock(), Log()
    selected_safe = safe_for_task(task_name)
    namespace = dict(Core=Core, time=clock, sleep=clock.sleep, random=random,
                     math=math, in_avoid=lambda *args: None,
                     logger=log, RuleClick=Rule, RuleLongClick=type('Long', (), {}),
                     RuleImage=Rule, RuleOcr=type('Ocr', (), {}),
                     GameStuckError=RuntimeError,
                     BASE_SAFE_AREA=selected_safe.BASE_SAFE_AREA,
                     get_settlement_safe_point=selected_safe.get_settlement_safe_point,
                     weighted_choice=lambda rules: rules[0],
                     GeneralInviteAssets=SimpleNamespace(I_GI_SURE=Rule(name='INVITE')))
    # Read the real constants and Timer, without importing logger / device modules.
    tree = ast.parse((ROOT / 'tasks/Component/GeneralBattle/reward_frame.py').read_text('utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                        'MULTI_CLICK_GAP_S', 'MULTI_CLICK_MAX_S',
                        'MULTI_CLICK_JITTER_PROB', 'MULTI_CLICK_JITTER_RANGE',
                        'MULTI_CLICK_SIZES', 'MULTI_CLICK_WEIGHTS', 'MULTI_CLICK_WEIGHTS_BY_EVENT',
                        'SETTLEMENT_CLICK_CD_S', 'SETTLEMENT_REUSE_TTL_S'):
                    namespace[target.id] = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == 'multi_click_weights':
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_weights', 'exec'), namespace)
    timer = ast.parse((ROOT / 'module/base/timer.py').read_text('utf-8-sig'))
    timer_class = next(n for n in timer.body if isinstance(n, ast.ClassDef) and n.name == 'Timer')
    exec(compile(ast.Module(body=[timer_class], type_ignores=[]), 'actual_timer', 'exec'), namespace)
    source_class(ROOT / 'tasks/base_task.py', 'BaseTask', 'Base', 'Core',
                 {'appear', 'appear_then_click', 'click', 'ui_click_until_disappear'}, namespace)
    source_class(ROOT / 'tasks/Component/GeneralBattle/general_battle.py',
                 'GeneralBattle', 'Parent', 'Base',
                 {'battle_wait', 'run_general_battle_back', 'settlement_click', 'settlement_click_grid',
                  'settlement_gesture', '_settlement_cd_ready', '_settlement_cd_touch',
                  'win_appear', 'win_template_names', 'reward_grid_appear',
                  'settlement_click_count', '_settlement_extra_clicks'}, namespace)
    realm = source_class(ROOT / 'tasks/RealmRaid/script_task.py', 'ScriptTask', 'Realm', 'Parent',
                         {'battle_wait', 'run_general_battle_back',
                          '_get_settlement_safe_point', '_log_settlement_safe_point',
                          '_click_settlement_safe_area', 'appear_then_click', 'click',
                          'settlement_gesture', '_settlement_point',
                          'settlement_click_count', '_settlement_extra_clicks', 'fire'}, namespace)
    if task_name == 'RealmRaid':
        return realm(clock, frames), clock, log
    ryou = source_class(HERE / 'script_task.py', 'ScriptTask', 'Ryou', 'Realm',
                        {'battle_wait', '_get_settlement_safe_point',
                         '_log_settlement_safe_point', '_click_settlement_safe_area',
                         'appear_then_click', 'settlement_gesture', '_settlement_point',
                         'settlement_click_count', '_settlement_extra_clicks'}, namespace)
    return ryou(clock, frames), clock, log


class SamplingTests(unittest.TestCase):
    TASK = 'RyouToppa'

    def test_10000_points(self):
        safe = safe_for_task(self.TASK)
        seed = 20261003 if self.TASK == 'RyouToppa' else 20261004
        rng = random.Random(seed)
        points = [safe.get_settlement_safe_point(rng) for _ in range(10000)]
        hits = [sum(safe.point_in_rect(x, y, rect) for x, y in points)
                for rect in safe.EXCLUDED_AREAS]
        self.assertEqual(hits, [0, 0, 0])
        self.assertTrue(all(safe.point_in_rect(x, y, safe.BASE_SAFE_AREA) for x, y in points))
        grid = [[0] * 20 for _ in range(20)]
        for x, y in points:
            grid[y // 36][x // 64] += 1
        largest = max(map(max, grid)) / len(points)
        self.assertLess(largest, 0.01)
        self.assertFalse(any(125 <= y <= 325 for _, y in points))
        self.assertTrue(any(y < 125 for _, y in points))
        self.assertTrue(any(y > 325 for _, y in points))
        result = dict(task=self.TASK, samples=len(points), seed=seed,
                      x_range=[min(x for x, _ in points), max(x for x, _ in points)],
                      y_range=[min(y for _, y in points), max(y for _, y in points)],
                      exclusion_hits=hits, grid_20x20=grid,
                      grid_cell_pixels=[64, 36], max_grid_share=largest)
        print('\nSAMPLING ' + json.dumps({k:v for k,v in result.items() if k != 'grid_20x20'}))
        if os.environ.get('RAID_VERIFY_OUT'):
            out = Path(os.environ['RAID_VERIFY_OUT'])
            (out / f'{self.TASK}_statistics.json').write_text(json.dumps(result, indent=2), 'utf-8')
            (out / f'{self.TASK}_points.json').write_text(json.dumps(points), 'utf-8')

    def test_inclusive_boundaries(self):
        safe = safe_for_task(self.TASK)
        for rect in (safe.BASE_SAFE_AREA,) + safe.EXCLUDED_AREAS:
            l, t, r, b = rect
            for point in ((l,t),(r,t),(l,b),(r,b)):
                self.assertTrue(safe.point_in_rect(*point, rect))
            for point in ((l-1,t),(r+1,t),(l,t-1),(l,b+1)):
                self.assertFalse(safe.point_in_rect(*point, rect))

    def test_reject_edges_then_accept_base_corner(self):
        safe = safe_for_task(self.TASK)
        rng = SimpleNamespace(randint=unittest.mock.Mock(
            side_effect=[20,125,1260,325,825,70,315,700,20,70]))
        self.assertEqual(safe.get_settlement_safe_point(rng), (20,70))
        self.assertEqual(rng.randint.call_count, 10)

    def test_bounded_failure(self):
        safe = safe_for_task(self.TASK)
        rng = SimpleNamespace(randint=unittest.mock.Mock(side_effect=[300,200]*64))
        with self.assertRaises(RuntimeError):
            safe.get_settlement_safe_point(rng)
        self.assertEqual(rng.randint.call_count,128)


class IntegrationTests(unittest.TestCase):
    TASK = 'RyouToppa'

    def make(self, frames=()):
        return runtime(frames, self.TASK)

    def assert_fresh(self, task):
        self.assertTrue(all(shot > 0 for shot in task.click_shots))
        self.assertEqual(len(task.click_shots),len(set(task.click_shots)))

    def assert_safe(self, task):
        safe = safe_for_task(self.TASK)
        for x,y,_ in task.events:
            self.assertTrue(safe.point_in_rect(x,y,safe.BASE_SAFE_AREA))
            self.assertFalse(any(safe.point_in_rect(x,y,r) for r in safe.EXCLUDED_AREAS))

    def test_actual_win_reward_state_machine(self):
        task,_,log = self.make([{'I_WIN'}, {'I_WIN'}, {'I_REWARD'},
                              {'I_REWARD'}, {'I_REWARD'}, {'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual(len(task.events),2)
        self.assert_safe(task)
        self.assertEqual(sum('settlement_safe_click state=win' in m for m in log.messages),1)
        self.assertEqual(sum('settlement_safe_click state=reward' in m for m in log.messages),1)
        self.assertEqual([e[2].get('pace',True) for e in task.events], [True,True])
        self.assert_fresh(task)
        self.assertFalse(task._ryou_settlement_active)

    def test_actual_failure_returns_false(self):
        task,_,log = self.make([{'I_FALSE'},{'I_FALSE'},{'I_TOPPA_RECORD'}])
        self.assertFalse(task.battle_wait(False))
        self.assertEqual(len(task.events),1)
        self.assert_safe(task)
        self.assertTrue(any('state=false' in m for m in log.messages))

    def test_win_variant_and_reward_grid_fallback(self):
        task,_,log = self.make([{'I_WIN_2'}, {'I_WIN_2'}, {'I_REWARD'},
                              {'I_REWARD'}, {'GRID'}, {'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual(len(task.events),2)
        self.assert_safe(task)
        self.assertEqual(task.events[-1][2]['control_name'],'REWARD_GRID')
        self.assertEqual(sum('state=reward' in m for m in log.messages),1)
        self.assert_fresh(task)

    def test_direct_de_win_confirmation(self):
        task,_,log = self.make([{'I_DE_WIN'}, {'I_DE_WIN'}, {'I_REWARD'},
                              {'I_REWARD'}, {'I_REWARD'}, {'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assert_safe(task)
        self.assertGreaterEqual(sum('state=win' in m for m in log.messages),1)
        self.assert_fresh(task)

    def test_nonsettlement_and_outside_wait_delegate(self):
        task,_,_ = self.make()
        for name,active in [('I_PREPARE_HIGHLIGHT',True),('I_FIRE',True),('I_FALSE',False)]:
            task._ryou_settlement_active=active
            task._realm_settlement_active=active
            rule=getattr(task,name)
            task.device.image.visible={name}
            self.assertTrue(task.appear_then_click(rule))
            self.assertEqual(task.events[-1][:2],(10,10))

    def test_false_interval_and_shared_cooldown(self):
        task,clock,_ = self.make()
        task._ryou_settlement_active=True
        task._realm_settlement_active=True
        task.device.image.visible={'I_FALSE'}
        self.assertTrue(task.appear_then_click(task.I_FALSE,interval=1.5))
        self.assertFalse(task.appear_then_click(task.I_FALSE,interval=1.5))
        clock.sleep(1.1)
        self.assertFalse(task.appear_then_click(task.I_FALSE,interval=1.5))
        clock.sleep(0.5)
        self.assertTrue(task.appear_then_click(task.I_FALSE,interval=1.5))
        self.assertEqual(len(task.events),2)

    def test_explicit_failure_action_delegates(self):
        task,_,_=self.make()
        task._ryou_settlement_active=True
        task._realm_settlement_active=True
        task.device.image.visible={'I_FALSE'}
        self.assertTrue(task.appear_then_click(task.I_FALSE,action=Rule(name='explicit')))
        self.assertEqual(task.events[0][:2],(10,10))

    def test_unmatched_false_does_not_click(self):
        task,_,_ = self.make()
        task._ryou_settlement_active=True
        task._realm_settlement_active=True
        self.assertFalse(task.appear_then_click(task.I_FALSE))
        self.assertEqual(task.events,[])

    def test_wrong_resolution_never_clicks(self):
        task,_,log=self.make()
        task.device.image.shape=(576,1024,3)
        with self.assertRaises(RuntimeError):
            task.settlement_gesture(None,task.I_WIN.name)
        self.assertEqual(task.events,[])
        self.assertTrue(any('unsupported frame' in m for m in log.messages))

    def test_sampling_failure_never_clicks(self):
        task,_,log=self.make()
        with patch.dict(task._get_settlement_safe_point.__globals__,
                        get_settlement_safe_point=lambda: (_ for _ in ()).throw(RuntimeError('exhausted'))):
            with self.assertRaises(RuntimeError):
                task.settlement_gesture(None,task.I_WIN.name)
        self.assertEqual(task.events,[])
        self.assertTrue(any('sampling exhausted' in m for m in log.messages))

    def test_extra_click_barrier(self):
        task,clock,_=self.make()
        task.n=4
        task._ryou_settlement_state='reward'
        start=clock.time()-10
        task._settlement_extra_clicks(None,20,70,'I_REWARD',first_ts=start)
        self.assertEqual(task.events,[])

    def test_wait_flag_restored_on_exception(self):
        task,_,_=self.make()
        parent=next(c for c in type(task).__mro__ if c.__name__=='Parent')
        with patch.object(parent,'battle_wait',side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError):
                task.battle_wait(False)
        self.assertFalse(task._ryou_settlement_active)
        self.assertFalse(task._realm_settlement_active)

    def test_existing_no_reward_timeout(self):
        task,_,_=self.make([{'I_WIN'},{'I_WIN'},set()])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual(task.wait_budget,5)

    def test_gesture_one_click_with_any_count_policy(self):
        for n in (1,2,3,4):
            with self.subTest(n=n):
                task,clock,log=self.make([{'I_REWARD'}])
                task.screenshot()
                self.assertEqual(task.settlement_click_count(0),1)
                first_ts=clock.time()
                parent=next(c for c in type(task).__mro__ if c.__name__=='Parent')
                with patch.object(parent,'settlement_click_count',return_value=n), \
                     patch.object(type(task),'settlement_click_count',return_value=n):
                    task.settlement_gesture(None,task.I_REWARD.name)
                self.assertEqual(len(task.events),1)
                self.assert_safe(task)
                self.assertEqual(task._settlement_page_clicks,1)
                self.assertEqual(task._settlement_click_ts,first_ts)
                self.assertEqual(task._settlement_last[2],first_ts)
                self.assertEqual(sum('settlement_safe_click state=reward' in m for m in log.messages),1)

    def test_parent_other_task_still_bursts(self):
        task,clock,_=self.make()
        parent=next(c for c in type(task).__mro__ if c.__name__=='Parent')
        other=parent(clock,[{'I_REWARD'}])
        other.screenshot()
        with patch.object(parent,'settlement_click_count',return_value=4):
            other.settlement_gesture(Rule(roi_front=(0,0,1280,720)),other.I_REWARD.name)
        self.assertEqual(len(other.events),4)

    def test_reward_persists_next_frame_can_click(self):
        task,_,_=self.make([{'I_WIN'},{'I_WIN'},{'I_REWARD'},
                           {'I_REWARD'},{'I_REWARD'},{'I_REWARD'},
                           {'I_REWARD'},{'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual(len(task.events),3)
        self.assert_fresh(task)

    def test_first_reward_click_then_list_has_no_extra_click(self):
        task,_,_=self.make([{'I_REWARD'},{'I_REWARD'},{'I_REWARD'},
                           {'I_REWARD'},{'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual(len(task.events),1)
        self.assert_fresh(task)

    def test_unknown_reward_reframes_before_confirm(self):
        task,_,_=self.make([{'I_WIN'},{'I_WIN'},{'I_REWARD'},{'I_REWARD'},
                           {'I_STATISTICS'},{'I_CONFIRM_CLOSE_DIFF_SOUL'},{'I_TOPPA_RECORD'}])
        self.assertTrue(task.battle_wait(False))
        self.assertEqual([e[2]['control_name'] for e in task.events],
                         ['I_WIN','I_STATISTICS','I_CONFIRM_CLOSE_DIFF_SOUL'])
        self.assertEqual(task.events[-1][:2],(10,10))
        self.assert_fresh(task)

    def test_retreat_operations_stay_precise(self):
        task,_,log=self.make([{'I_EXIT'},{'I_EXIT_ENSURE'},
                             {'I_EXIT_ENSURE'},{'I_FALSE'},{'I_FALSE'},{'I_TOPPA_RECORD'}])
        self.assertTrue(task.run_general_battle_back(SimpleNamespace(lock_team_enable=True),exit_four=True))
        self.assertEqual([e[2]['control_name'] for e in task.events],['I_EXIT','I_EXIT_ENSURE','I_FALSE'])
        self.assertEqual([e[:2] for e in task.events[:2]],[(10,10),(10,10)])
        self.assertTrue(any(f'[{self.TASK}]' in m and 'state=false' in m for m in log.messages))
        self.assert_fresh(task)

    def test_battle_random_keeps_original_path(self):
        task,_,log=self.make()
        task._realm_settlement_active=True
        task.device.image.visible={'I_STATISTICS'}
        task.click(task.C_RANDOM_CLICK,interval=20)
        self.assertEqual(task.events[0][:2],(10,10))
        self.assertFalse(any('settlement_safe_click' in m for m in log.messages))

    def test_precise_return_confirm_again_and_target_delegate(self):
        task,_,_=self.make()
        task._realm_settlement_active=True
        for name in ('I_EXIT','I_EXIT_ENSURE','I_AGAIN_BATTLE'):
            task.device.image.visible={name}
            self.assertTrue(task.appear_then_click(getattr(task,name)))
            self.assertEqual(task.events[-1][:2],(10,10))
        task.click(Rule(name='grid_target'))
        self.assertEqual(task.events[-1][:2],(10,10))

    def test_gesture_cooldown_keeps_counter_and_timestamp(self):
        task,_,_=self.make([{'I_REWARD'}]*2)
        task.screenshot()
        task.settlement_gesture(None,task.I_REWARD.name)
        stamp=task._settlement_click_ts
        task.settlement_gesture(None,task.I_REWARD.name)
        self.assertEqual(len(task.events),1)
        self.assertEqual(task._settlement_click_ts,stamp)
        self.assertEqual(task._settlement_page_clicks,1)
        task.screenshot()
        task.settlement_gesture(None,task.I_REWARD.name)
        self.assertEqual(len(task.events),2)
        self.assertEqual(task._settlement_page_clicks,2)

    def test_realm_fire_reward_cleanup_preserves_target_and_attack(self):
        if self.TASK != 'RealmRaid':
            self.skipTest('RealmRaid-specific fire path; RyouToppa uses attack_area')
        task,_,log=self.make([{'I_REWARD'},{'I_RR_PERSON'},
                             {'I_RR_PERSON'},{'I_FIRE'},{'I_PREPARE_HIGHLIGHT'}])
        task.I_RR_PERSON=Rule(name='I_RR_PERSON')
        task.I_FRESH_ENSURE=Rule(name='I_FRESH_ENSURE')
        task.I_SOUL_RAID=Rule(name='I_SOUL_RAID')
        task.partition=[Rule(name='grid_target')]
        task.ui_page_appear=lambda page: 'LIST' in task.device.image.visible
        task.is_in_prepare=lambda _: 'I_PREPARE_HIGHLIGHT' in task.device.image.visible
        task.is_in_real_battle=lambda _: False
        task.fire.__globals__['page_realm_raid']=object()
        task.fire(1)
        self.assertEqual([e[2]['control_name'] for e in task.events],
                         ['I_REWARD','grid_target','I_FIRE'])
        self.assertEqual([e[:2] for e in task.events[1:]],[(10,10),(10,10)])
        self.assertTrue(any('state=reward' in m for m in log.messages))
        self.assert_fresh(task)


if __name__ == '__main__':
    unittest.main(verbosity=2)
