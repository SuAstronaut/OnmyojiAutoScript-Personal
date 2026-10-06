"""Invitation regression: execute source methods with simulated OCR and input only.

Never import an OAS task, logger or Device, or open a user configuration. The
configuration model imports only Pydantic and the side-effect-free config_base.
"""
import ast
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from pydantic import BaseModel, Field
import pytest

from tasks.Component.config_base import Time
from tasks.Component.GeneralInvite.config_invite import FindMode, InviteConfig


ROOT = Path(__file__).resolve().parents[3]
SOURCES = {
    'general': ('tasks/Component/GeneralInvite/general_invite.py', 'GeneralInvite'),
    'exploration': ('tasks/Exploration/solo.py', 'SoloExploration'),
    'bondling': ('tasks/BondlingFairyland/general_invite.py', 'GeneralInvite'),
}
TITLES = ('好友', '最近', '寮友', '跨区')


def definition(source, name, class_name=None):
    tree = ast.parse(source)
    body = tree.body
    if class_name:
        body = next(node.body for node in body
                    if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in body if getattr(node, 'name', None) == name)


def compile_definition(node, namespace):
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<offline-source>', 'exec'), namespace)
    return namespace[node.name]


@lru_cache(maxsize=None)
def head_source(path):
    return subprocess.run(['git', 'show', 'HEAD:' + path], cwd=ROOT,
                          check=True, capture_output=True).stdout.decode('utf-8')


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class Log:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    warning = info


class Flag:
    def __init__(self, index, selected):
        self.index, self.selected = index, selected

    def match_mean_color(self, image, color, difference):
        assert color == (227, 193, 153) and difference == 10
        return image.on_visible and image.current == self.index


class Ocr:
    def __init__(self, text):
        self.text = text

    def ocr(self, image):
        return self.text


class Sim:
    INVITE_FLAG_ON = (227, 193, 153)
    friend_class = ['好友', '最近', '跨区', '寮友', '蔡友', '路区', '察友', '区']
    I_LOAD_FRIEND = 'load'
    I_INVITE_ENSURE = 'confirm'

    def __init__(self, titles, *, current=0, found_at=None, delay=0,
                 blocked=(), off_visible=True, on_visible=True, find_attempt=1):
        self.clock = Clock()
        self.log = Log()
        self.device = SimpleNamespace(image=SimpleNamespace(
            current=current, dialog=True, on_visible=on_visible))
        self.found_at, self.find_attempt = found_at, find_attempt
        self.delay, self.blocked, self.off_visible = delay, set(blocked), off_visible
        self.pending = None
        self.shots = 0
        self.clicks, self.detections = [], []
        self.last_click = {}
        self.confirmations = 0
        for index in range(4):
            setattr(self, f'O_F_LIST_{index + 1}', Ocr(titles[index] if index < len(titles) else ''))
            setattr(self, f'I_FLAG_{index + 1}_ON', Flag(index, True))
            setattr(self, f'I_FLAG_{index + 1}_OFF', Flag(index, False))
        self.O_FRIEND, self.O_KUAQU = Ocr(titles[0]), Ocr(titles[1])
        self.I_SELECT_FRIEND_ON, self.I_SELECT_FRIEND_OFF = Flag(0, True), Flag(0, False)
        self.I_SELECT_KUAQU_ON, self.I_SELECT_KUAQU_OFF = Flag(1, True), Flag(1, False)

    def screenshot(self):
        self.shots += 1
        assert self.shots < 300, 'Offline guard: unexpected unbounded loop'
        self.clock.sleep(0.25)
        if self.pending and self.clock.now >= self.pending[1]:
            self.device.image.current = self.pending[0]
            self.pending = None

    def appear(self, rule):
        if rule in ('load', 'confirm'):
            return self.device.image.dialog
        assert isinstance(rule, Flag)
        if rule.selected:
            return self.device.image.on_visible and self.device.image.current == rule.index
        return self.off_visible and self.device.image.current != rule.index

    def click(self, rule, interval=0):
        if rule == 'confirm':
            self.device.image.dialog = False
            self.confirmations += 1
            return True
        assert isinstance(rule, Flag) and not rule.selected
        if self.clock.now - self.last_click.get(rule.index, float('-inf')) < interval:
            return False
        self.last_click[rule.index] = self.clock.now
        self.clicks.append(rule.index)
        if rule.index not in self.blocked:
            if not self.pending or self.pending[0] != rule.index:
                self.pending = (rule.index, self.clock.now + self.delay)
        return True

    def appear_then_click(self, rule, interval=0):
        return self.appear(rule) and self.click(rule, interval)

    def detect_select(self, name):
        assert name == '目标队友'
        assert self.pending is None, 'Name search ran before the tab was selected'
        index = self.device.image.current
        self.detections.append(index)
        return index == self.found_at and self.detections.count(index) >= self.find_attempt

    @property
    def scanned(self):
        return [index for position, index in enumerate(self.detections)
                if position == 0 or index != self.detections[position - 1]]


def runtime(kind, *, baseline=False, titles=None, **kwargs):
    titles = titles or (('好友', '跨区') if kind == 'bondling' else TITLES)
    task = Sim(titles, **kwargs)

    class Timer:
        def __init__(self, seconds):
            self.seconds = seconds

        def start(self):
            self.started = task.clock.now
            return self

        def reached(self):
            return task.clock.now - self.started >= self.seconds

    namespace = {'FindMode': FindMode, 'Timer': Timer,
                 'sleep': task.clock.sleep, 'logger': task.log}
    helper_path = SOURCES['general'][0]
    compile_definition(definition((ROOT / helper_path).read_text(encoding='utf-8'),
                                  'find_cross_region_first'), namespace)
    path, class_name = SOURCES[kind]
    source = head_source(path) if baseline else (ROOT / path).read_text(encoding='utf-8')
    method = compile_definition(definition(source, 'invite_friend', class_name), namespace)
    task.invite_friend = method.__get__(task)
    return task


@pytest.mark.parametrize('kind', SOURCES)
@pytest.mark.parametrize('mode', [FindMode.AUTO_FIND, FindMode.RECENT_FRIEND, FindMode.CROSS_REGION_FIRST])
def test_source_order_and_one_scan_failure(kind, mode):
    task = runtime(kind)
    assert task.invite_friend('目标队友', mode) is False
    if kind == 'bondling':
        expected = {FindMode.AUTO_FIND: [0, 1], FindMode.RECENT_FRIEND: [],
                    FindMode.CROSS_REGION_FIRST: [1, 0]}[mode]
    else:
        expected = {FindMode.AUTO_FIND: [0, 1, 2, 3], FindMode.RECENT_FRIEND: [1],
                    FindMode.CROSS_REGION_FIRST: [3, 0, 1, 2]}[mode]
    assert task.scanned == expected
    assert task.detections == [index for index in expected for _ in range(2)]
    assert task.confirmations == (0 if kind == 'bondling' and mode == FindMode.RECENT_FRIEND else 1)


@pytest.mark.parametrize('kind', SOURCES)
@pytest.mark.parametrize('mode', [FindMode.AUTO_FIND, FindMode.CROSS_REGION_FIRST])
@pytest.mark.parametrize('find_attempt', [1, 2])
def test_found_target_stops_all_remaining_sources(kind, mode, find_attempt):
    order = ([0, 1] if mode == FindMode.AUTO_FIND else [1, 0]) if kind == 'bondling' else (
        [0, 1, 2, 3] if mode == FindMode.AUTO_FIND else [3, 0, 1, 2])
    task = runtime(kind, found_at=order[0], find_attempt=find_attempt)
    assert task.invite_friend('目标队友', mode) is True
    assert task.detections == [order[0]] * find_attempt
    assert task.confirmations == 1


@pytest.mark.parametrize('kind', SOURCES)
def test_cross_region_failure_falls_back_and_finds_friend(kind):
    task = runtime(kind, found_at=0)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is True
    assert task.scanned == ([1, 0] if kind == 'bondling' else [3, 0])


@pytest.mark.parametrize('kind', ['general', 'exploration'])
@pytest.mark.parametrize('missing', [0, 1, 2])
@pytest.mark.parametrize('missing_text', ['', None])
def test_ocr_missing_column_never_moves_physical_cross_region_tab(kind, missing, missing_text):
    titles = list(TITLES)
    titles[missing] = missing_text
    task = runtime(kind, titles=titles)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
    assert task.clicks[0] == 3
    assert task.scanned == [index for index in [3, 0, 1, 2] if index != missing]
    assert missing not in task.clicks


@pytest.mark.parametrize('kind', ['general', 'exploration'])
def test_cross_region_not_recognized_at_fourth_position_is_skipped(kind):
    task = runtime(kind, titles=('好友', '最近', '跨区', ''))
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
    assert task.scanned == [0, 1]
    assert 2 not in task.clicks and 3 not in task.clicks


@pytest.mark.parametrize('missing', [0, 1])
def test_bondling_missing_ocr_keeps_two_physical_positions(missing):
    titles = ['好友', '跨区']
    titles[missing] = None
    task = runtime('bondling', titles=titles)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
    assert task.scanned == [1 - missing]
    assert missing not in task.clicks


@pytest.mark.parametrize('kind', ['general', 'exploration'])
def test_existing_ocr_aliases_keep_physical_positions(kind):
    task = runtime(kind, titles=(' 好友、', '最近', '蔡友', ' 路区、'))
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
    assert task.scanned == [3, 0, 1, 2]


@pytest.mark.parametrize('kind', SOURCES)
def test_already_on_cross_region_does_not_click_again(kind):
    index = 1 if kind == 'bondling' else 3
    task = runtime(kind, current=index, found_at=index)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is True
    assert task.clicks == []


@pytest.mark.parametrize('kind', SOURCES)
def test_tab_switch_delay_is_confirmed_before_name_search(kind):
    index = 1 if kind == 'bondling' else 3
    task = runtime(kind, delay=1.5, found_at=index)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is True
    assert task.scanned == [index]
    assert task.clock.now >= 1.5


@pytest.mark.parametrize('kind', SOURCES)
def test_failed_cross_region_switch_is_bounded_and_skips_name_search(kind):
    index = 1 if kind == 'bondling' else 3
    task = runtime(kind, blocked=(index,), found_at=0)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is True
    assert task.scanned == [0]
    assert 5 <= task.clock.now < 8


@pytest.mark.parametrize('kind', SOURCES)
def test_unknown_tab_state_does_not_guess_click(kind):
    task = runtime(kind, off_visible=False, on_visible=False)
    assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
    assert task.clicks == [] and task.detections == []
    assert task.confirmations == 1


@pytest.mark.parametrize('kind', SOURCES)
def test_next_outer_invitation_starts_from_cross_region_again(kind):
    task = runtime(kind)
    for _ in range(2):
        task.device.image.dialog = True
        task.detections.clear()
        assert task.invite_friend('目标队友', FindMode.CROSS_REGION_FIRST) is False
        assert task.scanned[0] == (1 if kind == 'bondling' else 3)
    assert task.confirmations == 2


@pytest.mark.parametrize('kind', SOURCES)
@pytest.mark.parametrize('mode', [FindMode.AUTO_FIND, FindMode.RECENT_FRIEND])
@pytest.mark.parametrize('scenario', ['failure', 'success', 'missing_friend', 'missing_recent'])
def test_old_modes_match_head_behavior_including_existing_missing_ocr_paths(kind, mode, scenario):
    titles = list(('好友', '跨区') if kind == 'bondling' else TITLES)
    if scenario.startswith('missing_'):
        titles[0 if scenario == 'missing_friend' else 1] = ''
    kwargs = {'titles': titles, 'found_at': 1 if scenario == 'success' else None}
    old, new = runtime(kind, baseline=True, **kwargs), runtime(kind, **kwargs)
    assert new.invite_friend('目标队友', mode) == old.invite_friend('目标队友', mode)
    assert (new.clicks, new.detections, new.confirmations, new.shots, new.clock.now, new.log.messages) == (
        old.clicks, old.detections, old.confirmations, old.shots, old.clock.now, old.log.messages)


@pytest.fixture(params=['general', 'exploration'])
def config_model(request):
    if request.param == 'general':
        return InviteConfig
    source = (ROOT / 'tasks/Exploration/config.py').read_text(encoding='utf-8')
    return compile_definition(definition(source, 'InviteConfig'), {
        'BaseModel': BaseModel, 'Field': Field, 'FindMode': FindMode, 'Time': Time})


def test_config_default_and_schema_keep_old_values(config_model):
    assert config_model().find_mode == FindMode.AUTO_FIND
    schema = config_model.model_json_schema()
    assert schema['properties']['find_mode']['default'] == 'auto_find'
    assert schema['$defs']['FindMode']['enum'] == ['auto_find', 'recent_friend', 'cross_region_first']


@pytest.mark.parametrize('value', ['auto_find', 'recent_friend', 'cross_region_first'])
def test_config_parsing_and_json_round_trip(config_model, value):
    payload = {'friend_1': '目标队友', 'find_mode': value, 'wait_time': '00:02:00'}
    before = dict(payload)
    model = config_model.model_validate(payload)
    assert model.find_mode.value == value and payload == before
    assert config_model.model_validate_json(model.model_dump_json()).find_mode.value == value


def test_actual_args_builder_publishes_three_enum_values(config_model):
    class TaskConfig(BaseModel):
        invite_config: config_model = Field(default_factory=config_model)

    source = (ROOT / 'module/config/config_model.py').read_text(encoding='utf-8')
    method = compile_definition(definition(source, 'script_task', 'ConfigModel'), {
        're': re, 'convert_to_underscore': lambda value: value,
        'inflection': SimpleNamespace(underscore=lambda value: value)})
    host = SimpleNamespace(task=TaskConfig())
    for name in ('_inject_desktop_handle_options', '_filter_orochi_team_fields',
                 '_filter_activity_shikigami_fields', '_filter_master_disciple_fields'):
        setattr(host, name, lambda *args: None)
    args = method(host, 'task')
    field = next(item for item in args['invite_config'] if item['name'] == 'find_mode')
    assert field['enumEnum'] == ['auto_find', 'recent_friend', 'cross_region_first']
    assert field['default'] == 'auto_find'
    assert field['description'] == 'find_mode_help'


def test_tracked_chinese_additions_supply_label_and_accurate_help():
    source = (ROOT / 'module/server/i18n.py').read_text(encoding='utf-8')
    additions = compile_definition(definition(source, 'Addition'), {
        'Path': Path, 'json': json, 'extend': lambda hook, value, *args: value})
    additions.assets_i18n_dir = ROOT / 'assets/i18n'
    chinese = additions.load_additions()['zh-CN']
    assert chinese['cross_region_first'] == '跨区优先'
    help_text = chinese['find_mode_help']
    for text in ('好友 → 最近 → 寮友 → 跨区', '最近好友仅查最近',
                 '跨区 → 好友 → 最近 → 寮友', '每次扫描一轮', '契灵页面仅有好友和跨区'):
        assert text in help_text
    assert '快点' not in help_text
