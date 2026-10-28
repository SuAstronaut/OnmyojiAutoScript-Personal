"""Offline boss-exit regressions; never instantiate a game device.

State-machine tests use explicit boundary doubles, NOT recognition evidence.
Pixel tests replay untouched local screenshots through the production matcher.
Missing screenshot evidence is skipped and must be checked in-game by the user.
"""
import ast
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2]


class Stuck(Exception):
    pass


def methods(path, cls_name, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
    return {n.name: namespace[n.name] for n in nodes}


class Clock:
    now = 1.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def bind(task):
    clock = task.clock
    ns = dict(time=clock)
    tree = ast.parse((ROOT / 'module/base/timer.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Timer')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'production_timer', 'exec'), ns)
    base = ast.parse((ROOT / 'tasks/Exploration/base.py').read_text(encoding='utf-8'))
    node = next(n for n in base.body if isinstance(n, ast.ClassDef) and n.name == 'Scene')
    from enum import Enum
    ns['Enum'] = Enum
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'production_scene', 'exec'), ns)
    task.Scene = ns['Scene']
    ns.update(logger=Mock(), GameStuckError=Stuck,
              AutoRotate=SimpleNamespace(yes='yes'), ExplorationLevel=SimpleNamespace(EXPLORATION_28=28))
    loaded = methods('tasks/Exploration/base.py', 'BaseExploration',
                     {'quit_explore', '_quit_explore_after_boss', '_quit_solo_explore',
                      '_log_solo_exit_state', 'fire', 'get_current_scene'}, ns)
    loaded.update(methods('tasks/Exploration/solo.py', 'SoloExploration', {'run_solo'}, ns))
    for name, method in loaded.items():
        setattr(task, name, method.__get__(task, type(task)))


class BoundaryTask:
    """Logical boundary double: recognition booleans here are NOT real pixels."""
    def __init__(self, frames=('map',)):
        self.clock = Clock()
        self.frames = list(frames)
        self.shots = 0
        self.state = frames[0]
        self.events = []
        self.minions_cnt = 0
        self.current_count = 0
        self.config = SimpleNamespace(script=SimpleNamespace(error=SimpleNamespace(save_error=False)))
        self._config = SimpleNamespace(
            general_battle_config=SimpleNamespace(lock_team_enable=True),
            exploration_config=SimpleNamespace(auto_rotate='no', exploration_level=28))
        paths = ('tasks/Exploration/assets.py', 'tasks/GlobalGame/assets.py',
                 'tasks/GameUi/assets.py', 'tasks/Component/GeneralBattle/assets.py')
        for path in paths:
            tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                    name = node.targets[0].id
                    if name.startswith('I_'):
                        setattr(self, name, name)
        bind(self)
        self.get_current_scene = lambda **kwargs: getattr(self.Scene, {
            'map': 'MAIN', 'chest': 'MAIN', 'map_overlap_reward': 'MAIN',
            'entrance': 'ENTRANCE', 'world': 'WORLD',
            'battle': 'BATTLE_FIGHTING'}.get(self.state, 'UNKNOWN'))

    def screenshot(self):
        self.clock.sleep(0.25)
        self.state = self.frames[min(self.shots, len(self.frames) - 1)]
        self.shots += 1

    def appear(self, target, **kwargs):
        visible = {
            'map': {'I_E_SETTINGS_BUTTON', 'I_BACK_YOLLOW', 'I_UI_BACK_YELLOW', 'I_UI_BACK_BLUE'},
            'chest': {'I_E_SETTINGS_BUTTON', 'I_BACK_YOLLOW', 'I_BATTLE_REWARD'},
            'dialog': {'I_E_EXIT_CONFIRM', 'I_E_SETTINGS_BUTTON'},
            'reward': {'I_REWARD'},
            'map_overlap_reward': {'I_REWARD', 'I_E_SETTINGS_BUTTON', 'I_BACK_YOLLOW'},
            'entrance': {'I_UI_BACK_YELLOW', 'I_E_EXPLORATION_CLICK'},
            'world': {'I_CHECK_EXPLORATION'},
        }
        return target in visible.get(self.state, set())

    def appear_then_click(self, target, **kwargs):
        if not self.appear(target, **kwargs):
            return False
        self.events.append((target, self.state, self.clock.now))
        return True

    def is_in_prepare(self, is_screenshot=True):
        assert not is_screenshot
        return self.state == 'prepare'

    def is_in_battle(self, is_screenshot=True):
        assert not is_screenshot
        return self.state == 'battle'

    def win_appear(self):
        return self.state == 'win'

    def reward_grid_appear(self):
        return self.state == 'reward_grid'


@pytest.mark.parametrize('frame', ['map', 'chest'])
def test_boss_exit_clicks_map_back_but_never_paper_doll(frame):
    task = BoundaryTask([frame, 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=True)
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW']
    assert task.shots == 3 and task.minions_cnt == task.current_count == 0


def test_map_exit_confirmation_after_back_then_departure():
    task = BoundaryTask(['chest', 'dialog', 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=True)
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM']


def test_existing_shared_exit_still_collects_paper_doll_by_default():
    task = BoundaryTask(['chest', 'entrance'])
    task.quit_explore()
    assert [e[0] for e in task.events] == ['I_BATTLE_REWARD']


def test_transition_from_remaining_settlement_waits_before_map_exit():
    task = BoundaryTask(['win', 'reward', 'reward_grid', 'chest', 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=True)
    assert [e[:2] for e in task.events] == [('I_BACK_YOLLOW', 'chest')]
    assert task.shots == 6


@pytest.mark.parametrize('state', ['unknown', 'battle', 'prepare', 'win',
                                  'reward', 'reward_grid', 'map_overlap_reward'])
def test_unproven_map_or_settlement_never_clicked_or_reported_success(state):
    task = BoundaryTask([state])
    with pytest.raises(Stuck, match='before confirmed departure'):
        task.quit_explore(skip_battle_reward=True)
    assert not task.events and 20 <= task.clock.now - 1 <= 20.5


def test_failed_exit_has_total_timeout_and_shared_back_cooldown():
    task = BoundaryTask(['map'])
    with pytest.raises(Stuck):
        task.quit_explore(skip_battle_reward=True)
    assert 1 <= len(task.events) <= 6
    assert all(b[2] - a[2] >= 3.5 for a, b in zip(task.events, task.events[1:]))
    assert all(e[0] == 'I_BACK_YOLLOW' for e in task.events)


@pytest.mark.parametrize('destination', ['entrance', 'world'])
def test_already_outside_never_reclicks_back(destination):
    task = BoundaryTask([destination])
    task.quit_explore(skip_battle_reward=True)
    assert not task.events and task.shots == 2


def test_one_outside_frame_is_insufficient_and_unknown_resets_stability():
    task = BoundaryTask(['map', 'entrance', 'unknown', 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=True)
    assert task.shots == 5 and len(task.events) == 1


@pytest.mark.parametrize('win,require_win,expected', [(True, True, True), (False, True, False),
                                                    (True, False, True), (False, False, True)])
def test_fire_preserves_normal_battle_count_and_only_boss_requires_win(win, require_win, expected):
    task = BoundaryTask(['battle'])
    task.appear_then_click = lambda target, **kwargs: True
    def battle(config):
        assert config is task._config.general_battle_config
        task.events.extend(['battle', 'normal_settlement'])
        task.current_count += 1
        return win
    task.run_general_battle = battle
    assert task.fire(task.I_BOSS_BATTLE_BUTTON, require_win=require_win) is expected
    assert task.events == ['battle', 'normal_settlement']
    assert task.minions_cnt == task.current_count == 1


def test_boss_not_clicked_does_not_enter_battle_or_count():
    task = BoundaryTask()
    assert task.fire(task.I_BOSS_BATTLE_BUTTON, require_win=True) is False
    assert task.shots == task.minions_cnt == task.current_count == 0


class WorkflowTask(BoundaryTask):
    """Test actual run_solo/fire/quit dispatch with simulated device boundaries."""
    def __init__(self, quota=2, boss_win=True, exit_fails=False):
        super().__init__()
        self.quota = quota
        self.boss_win = boss_win
        self.exit_fails = exit_fails
        self.stage = 'normal'
        self.chapter = 1
        self.events = []

    def screenshot(self):
        self.clock.sleep(0.25)
        self.shots += 1
        if self.shots > 400:
            raise AssertionError('Unbounded workflow')

    def appear(self, target, **kwargs):
        if target == 'I_BOSS_BATTLE_BUTTON':
            return self.state == 'map' and self.stage == 'boss'
        return super().appear(target, **kwargs)

    def appear_then_click(self, target, **kwargs):
        if target == 'normal' or target == 'I_BOSS_BATTLE_BUTTON':
            self.state = 'battle'
        elif target == 'I_BACK_YOLLOW' and self.state == 'map':
            if not self.exit_fails:
                self.state = 'dialog'
        elif target == 'I_E_EXIT_CONFIRM' and self.state == 'dialog':
            self.state = 'entrance'
        else:
            return False
        self.events.append((target, self.minions_cnt))
        return True

    def run_general_battle(self, config):
        self.current_count += 1
        self.events.extend([('battle', self.stage), ('normal_settlement', self.stage)])
        self.state = 'map'
        if self.stage == 'normal':
            self.stage = 'boss'
            return True
        if not self.boss_win:
            # End this loss test without inventing a post-loss game flow.
            self.events.append(('boss_lost', self.minions_cnt))
            return False
        self.stage = 'done'
        return True

    def ui_click(self, click, **kwargs):
        self.events.append(('ui_click', click))
        if click == 'I_E_EXPLORATION_CLICK':
            self.chapter += 1
            self.state, self.stage = 'map', 'normal'
        return True

    def check_exit(self):
        self.events.append(('check_exit', self.state, self.minions_cnt))
        if not self.boss_win and self.minions_cnt == 2:
            raise StopIteration('boss-loss boundary')
        return self.minions_cnt >= self.quota

    def search_up_fight(self):
        return 'normal' if self.stage == 'normal' else None


def test_normal_win_stays_on_map_boss_win_exits_after_normal_settlement():
    task = WorkflowTask()
    task.run_solo()
    assert task.minions_cnt == task.current_count == 2
    normal_reward = task.events.index(('normal_settlement', 'normal'))
    boss_reward = task.events.index(('normal_settlement', 'boss'))
    back = task.events.index(('I_BACK_YOLLOW', 2))
    assert normal_reward < boss_reward < back
    assert ('I_E_EXIT_CONFIRM', 2) in task.events
    assert task.events[-1] == ('check_exit', 'entrance', 2)


def test_continuous_exploration_reinitializes_next_chapter_and_preserves_counts():
    task = WorkflowTask(quota=4)
    task.run_solo()
    assert task.chapter == 2 and task.current_count == task.minions_cnt == 4
    assert [e for e in task.events if e[0] == 'I_BACK_YOLLOW'] == [('I_BACK_YOLLOW', 2), ('I_BACK_YOLLOW', 4)]
    assert len([e for e in task.events if e == ('ui_click', 'I_E_UNLOCK')]) == 2
    assert task.events[-1] == ('check_exit', 'entrance', 4)


def test_boss_loss_does_not_take_post_boss_exit_path():
    task = WorkflowTask(boss_win=False)
    with pytest.raises(StopIteration, match='boss-loss boundary'):
        task.run_solo()
    assert not any(e[0] in ('I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM') for e in task.events)


def test_exit_failure_does_not_reach_success_check_or_next_chapter():
    task = WorkflowTask(exit_fails=True)
    with pytest.raises(Stuck):
        task.run_solo()
    assert task.chapter == 1 and task.current_count == task.minions_cnt == 2
    assert not any(e[0] == 'check_exit' and e[-1] == 2 for e in task.events)


def pixel_task(paths):
    """Use production RuleImage methods/assets and unaltered original screenshots."""
    task = BoundaryTask()
    ns = dict(cv2=cv2, np=np, fromfile=np.fromfile, uint8=np.uint8,
              float32=np.float32, int32=np.int32, Path=Path, cached_property=cached_property,
              RuleImageMallResourceMixin=object, logger=Mock(), get_current_humanizer=lambda: None)
    tree = ast.parse((ROOT / 'module/atom/image.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RuleImage')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'production_image_matcher', 'exec'), ns)
    for path in ('tasks/Exploration/assets.py', 'tasks/GlobalGame/assets.py',
                 'tasks/GameUi/assets.py', 'tasks/Component/GeneralBattle/assets.py',
                 'tasks/Component/GeneralRoom/assets.py',
                 'tasks/Component/GeneralInvite/assets.py'):
        tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        node.bases = []
        node.body = [n for n in node.body if isinstance(n, ast.Assign)
                     and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                     and n.value.func.id == 'RuleImage']
        exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), ns)
        for name, value in vars(ns[node.name]).items():
            if name.startswith('I_'):
                setattr(task, name, value)
    images = []
    for path in paths:
        if not path.exists():
            pytest.skip('Original local screenshot unavailable: ' + str(path))
        bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert bgr is not None
        images.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    task.device = SimpleNamespace(image=images[0])
    def screenshot():
        task.clock.sleep(0.25)
        task.device.image = images[min(task.shots, len(images) - 1)]
        task.shots += 1
    task.screenshot = screenshot
    task.appear = lambda rule, **kwargs: rule.match(task.device.image, threshold=kwargs.get('threshold'))
    task.appear_then_click = lambda rule, **kwargs: (
        task.events.append((rule.name, tuple(rule.roi_front), task.clock.now)) or True
        if task.appear(rule, **kwargs) else False)
    bind(task)
    battle_ns = dict(GeneralInviteAssets=SimpleNamespace(I_GI_SURE=None))
    for name, method in methods('tasks/Component/GeneralBattle/general_battle.py', 'GeneralBattle',
                                {'is_in_prepare', 'is_in_battle', 'win_appear'}, battle_ns).items():
        setattr(task, name, method.__get__(task, type(task)))
    for name, method in methods('tasks/Component/GeneralInvite/general_invite.py', 'GeneralInvite',
                                {'is_in_room'}, {}).items():
        setattr(task, name, method.__get__(task, type(task)))
    # Production visual reward-frame detector, with no recognition stubs.
    from tasks.Component.GeneralBattle.reward_frame import get_detector
    detector = get_detector()
    task.reward_grid_appear = lambda: bool(detector.detect(task.device.image))
    return task


MAP_SHOTS = [ROOT / 'log/error/1791605090915/2026-10-10_12-04-50-825749.png',
             ROOT / 'log/error/1791606604279/2026-10-10_12-30-04-097322.png']
NON_MAP = ROOT / 'log/error/1791609630531/2026-10-10_13-20-30-182949.png'


@pytest.mark.parametrize('path', MAP_SHOTS)
def test_real_map_recognition_and_back_resource_bounded_replay(path):
    task = pixel_task([path])
    assert task.get_current_scene() == task.Scene.MAIN
    assert task.appear(task.I_BACK_YOLLOW)
    matched_back_roi = tuple(task.I_BACK_YOLLOW.roi_front)
    with pytest.raises(Stuck):
        task.quit_explore(skip_battle_reward=True)
    assert 1 <= len(task.events) <= 6
    assert all(e[0] == task.I_BACK_YOLLOW.name for e in task.events)
    assert all(e[1] == matched_back_roi for e in task.events)


def test_real_courtyard_never_clicked_or_reported_as_map_exit_success():
    task = pixel_task([NON_MAP])
    with pytest.raises(Stuck):
        task.quit_explore(skip_battle_reward=True)
    assert not task.events
