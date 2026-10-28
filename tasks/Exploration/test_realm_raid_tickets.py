"""Offline regressions for the production Exploration path; no game device.

Boundary doubles prove dispatch/scheduling, not pixel recognition. Original
screenshots separately exercise production scene, icon and OCR implementations.
ConfigStore tests write only to pytest's temporary directory.
"""
import ast
import json
import sys
from datetime import datetime, timedelta
from enum import Enum
from functools import cached_property
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from tasks.Exploration.test_boss_exit import (
    ROOT, BoundaryTask, WorkflowTask, Stuck, methods, pixel_task,
)


class End(Exception):
    pass


@pytest.fixture(scope='session', autouse=True)
def offline_runtime():
    # Avoid the logger's default file output and initialize ORT before pyzmq.
    import onnxruntime  # noqa: F401
    previous = sys.modules.get('module.logger')
    module = ModuleType('module.logger')
    module.logger = Mock()
    sys.modules['module.logger'] = module
    yield
    if previous is None:
        sys.modules.pop('module.logger', None)
    else:
        sys.modules['module.logger'] = previous


def bind_tickets(task, threshold=27, count=0, cd=300):
    task._config.scrolls = SimpleNamespace(
        scrolls_enable=True, scrolls_threshold=threshold,
        scrolls_cd=timedelta(seconds=cd))
    con = task._config.exploration_config
    con.minions_cnt = 300000
    for name in ('buff_gold_50_click', 'buff_gold_100_click',
                 'buff_exp_50_click', 'buff_exp_100_click'):
        setattr(con, name, False)
    task.start_time = datetime.now()
    task.limit_time = timedelta(hours=23)
    task.O_REALM_RAID_NUMBER = Mock()
    task.O_REALM_RAID_NUMBER1 = Mock()
    for rule in (task.O_REALM_RAID_NUMBER, task.O_REALM_RAID_NUMBER1):
        rule.ocr.return_value = (count, 30 - count, 30)
    if not hasattr(task, 'device'):
        task.device = SimpleNamespace(image=None)
    task.ui_goto = Mock(return_value=True)
    task.set_next_run = Mock()
    task.ticket_logger = Mock()
    ns = dict(Scene=task.Scene, datetime=datetime, logger=task.ticket_logger, page_main='page_main',
              TaskEnd=End, GameStuckError=Stuck, RequestHumanTakeover=KeyboardInterrupt)
    for name, method in methods('tasks/Exploration/base.py', 'BaseExploration', {
            '_log_realm_raid_skip', '_realm_raid_ticket_rule',
            'activate_realm_raid', 'check_exit'}, ns).items():
        setattr(task, name, method.__get__(task, type(task)))


class TicketTask(BoundaryTask):
    def __init__(self, state='world', threshold=27, count=0, cd=300):
        super().__init__([state])
        self.icon_visible = True
        self.overlay = None
        bind_tickets(self, threshold, count, cd)

    def appear(self, target, **kwargs):
        if self.overlay == target:
            return True
        if target == 'I_E_REALM_RAID_TICKET_WORLD':
            return self.icon_visible and self.state == 'world'
        if target == 'I_E_REALM_RAID_TICKET_ENTRANCE':
            return self.icon_visible and self.state == 'entrance'
        if target in ('I_E_ENTRANCE', 'I_EXP_CREATE_TEAM'):
            return self.state == 'entrance'
        return super().appear(target, **kwargs)


def assert_no_link(task):
    task.ui_goto.assert_not_called()
    task.set_next_run.assert_not_called()


@pytest.mark.parametrize('state', ['world', 'entrance'])
@pytest.mark.parametrize('threshold,count,linked', [
    (20, 19, False), (20, 20, True), (27, 26, False), (27, 27, True),
    (27, 30, True), (30, 29, False), (30, 30, True), (27, 0, False),
])
def test_actual_check_exit_dynamic_threshold(state, threshold, count, linked):
    task = TicketTask(state, threshold, count)
    if linked:
        with pytest.raises(End):
            task.check_exit()
        task.ui_goto.assert_called_once_with('page_main')
        assert [c.kwargs['task'] for c in task.set_next_run.call_args_list] == [
            'Exploration', 'RealmRaid', 'MemoryScrolls']
    else:
        assert task.check_exit() is False
        assert_no_link(task)
    selected, other = ((task.O_REALM_RAID_NUMBER, task.O_REALM_RAID_NUMBER1)
                       if state == 'world' else
                       (task.O_REALM_RAID_NUMBER1, task.O_REALM_RAID_NUMBER))
    selected.ocr.assert_called_once()
    other.ocr.assert_not_called()


@pytest.mark.parametrize('state', ['map', 'battle', 'prepare', 'win',
                                  'reward', 'reward_grid', 'unknown'])
@pytest.mark.parametrize('result', [(496, 4, 500), (496, -446, 50), (27, 3, 30)])
def test_wrong_scene_never_reads_ticket_ocr(state, result):
    task = TicketTask(state, count=27)
    task.O_REALM_RAID_NUMBER.ocr.return_value = result
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    task.O_REALM_RAID_NUMBER1.ocr.assert_not_called()
    assert_no_link(task)


@pytest.mark.parametrize('state', ['world', 'entrance'])
@pytest.mark.parametrize('result', [
    (496, -446, 50), (496, 4, 500), (0, 0, 0), None, '', 'garbage',
    (31, -1, 30), (-1, 31, 30), (27, 2, 30), (27, 3),
    ('27', 3, 30), (True, 29, 30),
])
def test_invalid_ocr_never_navigates_or_schedules(state, result):
    task = TicketTask(state)
    for rule in (task.O_REALM_RAID_NUMBER, task.O_REALM_RAID_NUMBER1):
        rule.ocr.return_value = result
    assert task.check_exit() is False
    assert_no_link(task)
    assert 'invalid' in task.ticket_logger.info.call_args.args[0]


def test_ocr_exception_skips_and_duplicate_reason_is_logged_once():
    task = TicketTask()
    task.O_REALM_RAID_NUMBER.ocr.side_effect = RuntimeError('offline OCR failure')
    assert task.check_exit() is False
    assert task.check_exit() is False
    assert_no_link(task)
    assert task.ticket_logger.info.call_count == 1


@pytest.mark.parametrize('state', ['world', 'entrance'])
def test_unconfirmed_icon_or_layout_blocks_even_legal_numbers(state):
    task = TicketTask(state, count=30)
    task.icon_visible = False
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    task.O_REALM_RAID_NUMBER1.ocr.assert_not_called()
    assert_no_link(task)


@pytest.mark.parametrize('overlay', ['I_REWARD', 'I_REWARD_GOLD', 'I_WIN',
                                    'I_E_EXIT_CONFIRM', 'I_E_SETTINGS_BUTTON'])
def test_underlying_world_anchor_does_not_override_overlay(overlay):
    task = TicketTask(count=30)
    if overlay == 'I_WIN':
        task.win_appear = lambda: True
    else:
        task.overlay = overlay
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    assert_no_link(task)


def test_transition_before_fresh_frame_blocks_ocr():
    task = TicketTask(count=30)
    task.frames = ['map']
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    assert_no_link(task)


def test_disabled_switch_never_detects_or_navigates():
    task = TicketTask(count=30)
    task._config.scrolls.scrolls_enable = False
    task.get_current_scene = Mock(side_effect=AssertionError('scene detection forbidden'))
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    assert_no_link(task)


def test_existing_config_refresh_disable_during_confirmation_blocks_ocr():
    task = TicketTask(count=30)
    def refresh_frame():
        task._config.scrolls.scrolls_enable = False
    task.screenshot = refresh_frame
    assert task.check_exit() is False
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    assert_no_link(task)


@pytest.mark.parametrize('condition', ['count', 'time'])
def test_main_still_honors_normal_exit_conditions(condition):
    task = TicketTask('map')
    if condition == 'count':
        task.minions_cnt = task._config.exploration_config.minions_cnt
    else:
        task.start_time -= timedelta(days=1)
    assert task.check_exit() is True
    task.O_REALM_RAID_NUMBER.ocr.assert_not_called()
    assert_no_link(task)


@pytest.mark.parametrize('threshold', [None, 'bad', True])
def test_invalid_runtime_threshold_has_no_fallback(threshold):
    task = TicketTask(threshold=threshold, count=30)
    assert task.check_exit() is False
    assert_no_link(task)
    assert 'scrolls_threshold' in task.ticket_logger.info.call_args.args[0]


def test_schema_accepted_integer_thresholds_are_not_rewritten():
    task = TicketTask(threshold=31, count=30)
    assert task.check_exit() is False
    assert task._config.scrolls.scrolls_threshold == 31
    task._config.scrolls.scrolls_threshold = 0
    with pytest.raises(End):
        task.check_exit()


def test_navigation_failure_stops_current_linkage_without_scheduling():
    task = TicketTask(count=27)
    task.ui_goto.return_value = False
    with pytest.raises(Stuck, match='scheduling aborted'):
        task.check_exit()
    task.ui_goto.assert_called_once_with('page_main')
    task.set_next_run.assert_not_called()


@pytest.mark.parametrize('cd', [300, 900])
def test_existing_schedule_contract_and_configured_cd(cd):
    task = TicketTask(count=27, cd=cd)
    before = datetime.now()
    with pytest.raises(End):
        task.check_exit()
    after = datetime.now()
    calls = [c.kwargs for c in task.set_next_run.call_args_list]
    assert before + timedelta(seconds=cd) <= calls[0]['target'] <= after + timedelta(seconds=cd)
    assert calls[0]['success'] is None and calls[0]['finish'] is False
    assert calls[1]['server'] is False
    for call in calls[1:]:
        assert before <= call['target'] <= after
        assert call['success'] is None and call['finish'] is False


class TicketWorkflow(WorkflowTask):
    def __init__(self, destination='entrance', threshold=27):
        super().__init__()
        self.destination = destination
        bind_tickets(self, threshold, count=26)
        for rule in (self.O_REALM_RAID_NUMBER, self.O_REALM_RAID_NUMBER1):
            rule.ocr.side_effect = lambda image: (27, 3, 30) if self.minions_cnt >= 2 else (26, 4, 30)

    def appear(self, target, **kwargs):
        if target == 'I_E_REALM_RAID_TICKET_WORLD':
            return self.state == 'world'
        if target == 'I_E_REALM_RAID_TICKET_ENTRANCE':
            return self.state == 'entrance'
        if target in ('I_E_ENTRANCE', 'I_EXP_CREATE_TEAM'):
            return self.state == 'entrance'
        return super().appear(target, **kwargs)

    def appear_then_click(self, target, **kwargs):
        result = super().appear_then_click(target, **kwargs)
        if target == 'I_E_EXIT_CONFIRM' and result:
            self.state = self.destination
        return result

    def ui_click(self, click, **kwargs):
        result = super().ui_click(click, **kwargs)
        if self.chapter == 2:
            raise StopIteration('next natural chapter entered')
        return result


@pytest.mark.parametrize('destination', ['world', 'entrance'])
def test_actual_solo_boss_exit_checks_tickets_before_next_chapter(destination):
    task = TicketWorkflow(destination)
    with pytest.raises(End):
        task.run_solo()
    assert task.current_count == task.minions_cnt == 2
    assert task.chapter == 1
    assert ('normal_settlement', 'boss') in task.events
    assert ('I_E_EXIT_CONFIRM', 2) in task.events
    selected = task.O_REALM_RAID_NUMBER if destination == 'world' else task.O_REALM_RAID_NUMBER1
    selected.ocr.assert_called_once()
    task.ui_goto.assert_called_once_with('page_main')


def test_below_threshold_enters_next_chapter_naturally():
    task = TicketWorkflow(threshold=30)
    with pytest.raises(StopIteration, match='next natural chapter'):
        task.run_solo()
    task.O_REALM_RAID_NUMBER1.ocr.assert_called()
    assert_no_link(task)


def test_actual_main_paper_doll_then_normal_and_boss_settlement():
    task = TicketWorkflow()
    task.state = 'chest'
    def collect_reward(rule):
        task.events.append(('paper_doll', task.minions_cnt))
        task.state = 'map'
        return True
    task.ui_get_reward = Mock(side_effect=collect_reward)
    with pytest.raises(End):
        task.run_solo()
    task.ui_get_reward.assert_called_once_with('I_BATTLE_REWARD')
    assert task.events.index(('paper_doll', 0)) < task.events.index(('normal_settlement', 'normal'))
    assert task.events.index(('normal_settlement', 'normal')) < task.events.index(('normal_settlement', 'boss'))


@pytest.mark.parametrize('count', [26, 27])
def test_actual_solo_world_checks_before_opening_chapter(count):
    task = TicketTask(count=count)
    task.open_expect_level = Mock(side_effect=StopIteration('open next chapter'))
    task.ui_click = Mock(return_value=True)
    if count == 27:
        with pytest.raises(End):
            task.run_solo()
        task.open_expect_level.assert_not_called()
    else:
        with pytest.raises(StopIteration, match='open next chapter'):
            task.run_solo()
        task.O_REALM_RAID_NUMBER.ocr.assert_called_once()
        assert_no_link(task)


def test_real_config_load_reload_and_instance_isolation(tmp_path):
    from module.config.config import Config
    from module.config.config_store import ConfigStore
    raw = json.loads((ROOT / 'config/模拟器.json').read_text(encoding='utf-8'))
    for name, threshold in [('ticket_a', 20), ('ticket_b', 30)]:
        raw['config_name'] = name
        raw['exploration']['scrolls']['scrolls_threshold'] = threshold
        (tmp_path / (name + '.json')).write_text(json.dumps(raw), encoding='utf-8')
    store = ConfigStore(config_root=tmp_path)
    config_a = Config('ticket_a', store=store)
    config_b = Config('ticket_b', store=store)
    config_property = methods('tasks/Exploration/base.py', 'BaseExploration', {'_config'},
                              dict(cached_property=cached_property, timedelta=timedelta))['_config']
    config_property.__set_name__(TicketTask, '_config')
    def bind_config(task, config):
        task.config = config
        del task._config
        return config_property.__get__(task, type(task))
    task = TicketTask(count=20)
    cached_config = bind_config(task, config_a)
    with pytest.raises(End):
        task.check_exit()
    store.patch_user_argument('ticket_a', 'Exploration', 'scrolls', 'scrolls_threshold', 27)
    assert config_a.refresh_hot_at_checkpoint(task) is True
    assert task._config is cached_config
    assert cached_config.scrolls.scrolls_threshold == 27
    task.ui_goto.reset_mock()
    task.set_next_run.reset_mock()
    assert task.check_exit() is False
    assert_no_link(task)
    config_a.reload()
    new_task = TicketTask(count=20)
    bind_config(new_task, config_a)
    assert new_task.check_exit() is False
    assert_no_link(new_task)
    assert config_a.model.exploration.scrolls.scrolls_threshold == 27
    assert Config('ticket_a', store=store).model.exploration.scrolls.scrolls_threshold == 27
    assert config_b.model.exploration.scrolls.scrolls_threshold == 30
    assert store.load('ticket_b').model.exploration.scrolls.scrolls_threshold == 30
    with pytest.raises(ValueError):
        store.patch_user_argument('ticket_a', 'Exploration', 'scrolls', 'scrolls_threshold', 'bad')
    assert store.load('ticket_a').model.exploration.scrolls.scrolls_threshold == 27


@pytest.fixture(scope='module')
def local_ocr():
    import re
    import time
    from module.ocr.rapid_ocr import RapidOcrModel
    from module.ocr.result import BoxedResult
    model = RapidOcrModel(str(ROOT / 'toolkit/ocr_models'), device='cpu', model_type='small')
    ns = dict(Enum=Enum, Any=object, np=np, cv2=cv2, re=re, time=time,
              timedelta=timedelta, cached_property=cached_property, logger=Mock(),
              BoxedResult=BoxedResult, ScriptError=RuntimeError,
              float2str=lambda n: f'{n:.3f}', get_ocr_model=lambda lang: model)
    for path, names in [
        ('module/ocr/base_ocr.py', {'OcrMode', 'OcrMethodType', 'OcrMethod', 'BaseCor'}),
        ('module/ocr/sub_ocr.py', {'Full', 'Single', 'Digit', 'DigitCounter', 'Duration', 'Quantity'}),
        ('module/atom/ocr.py', {'RuleOcr'}),
    ]:
        tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), ns)
    return ns


SHOTS = Path('D:/Documents/MuMu共享文件夹/Screenshots')


@pytest.mark.parametrize('name,scene,selected', [
    ('MuMu-20261010-133758-411.png', 'WORLD', 'O_REALM_RAID_NUMBER'),
    ('MuMu-20261010-133801-620.png', 'ENTRANCE', 'O_REALM_RAID_NUMBER1'),
    ('MuMu-20261010-133907-971.png', 'MAIN', None),
])
def test_original_screenshot_actual_check_exit(name, scene, selected, local_ocr):
    task = pixel_task([SHOTS / name])
    bind_tickets(task)
    tree = ast.parse((ROOT / 'tasks/Exploration/assets.py').read_text(encoding='utf-8'))
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            key = n.targets[0].id
            if key in ('O_REALM_RAID_NUMBER', 'O_REALM_RAID_NUMBER1'):
                rule = eval(compile(ast.Expression(n.value), 'actual_ocr_asset', 'eval'), local_ocr)
                rule.ocr = Mock(wraps=rule.ocr)
                setattr(task, key, rule)
    assert task.get_current_scene().name == scene
    assert task.check_exit() is False
    assert_no_link(task)
    for key in ('O_REALM_RAID_NUMBER', 'O_REALM_RAID_NUMBER1'):
        rule = getattr(task, key)
        if key == selected:
            rule.ocr.assert_called_once()
            assert rule.ocr._mock_wraps(task.device.image) == (0, 30, 30)
        else:
            rule.ocr.assert_not_called()


def test_icon_files_are_exact_original_crops_and_metadata_agree():
    definitions = json.loads((ROOT / 'tasks/Exploration/res/image.json').read_text(encoding='utf-8'))
    for asset, source, box in [
        ('e_realm_raid_ticket_world', 'MuMu-20261010-133758-411.png', (686, 10, 33, 37)),
        ('e_realm_raid_ticket_entrance', 'MuMu-20261010-133801-620.png', (900, 7, 32, 37)),
    ]:
        image = cv2.imdecode(np.fromfile(SHOTS / source, np.uint8), cv2.IMREAD_COLOR)
        saved = cv2.imread(str(ROOT / 'tasks/Exploration/res' / (asset + '.png')))
        x, y, w, h = box
        assert np.array_equal(saved, image[y:y+h, x:x+w])
        definition = next(row for row in definitions if row['itemName'] == asset)
        assert definition['roiFront'] == ','.join(map(str, box))
