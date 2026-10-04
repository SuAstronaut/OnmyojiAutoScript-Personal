"""Offline regressions for the simplified SixRealms flow.

Execute the actual task methods, image matcher and OCR code in an AST harness.
No production logger/device imports, game captures, input or live state writes.
All persisted flags are confined to TemporaryDirectory. Generated frames and
level overlays are synthetic test inputs, never claimed as real game evidence.
"""
import ast
import json
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from enum import Enum
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import Mock, call
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'dev_tools/fixtures/sixrealms'
LOG = Mock()


def definitions(relative, namespace, names=None):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and (names is None or node.name in names)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / relative), 'exec'), namespace)


image_ns = dict(cv2=cv2, np=np, fromfile=np.fromfile, uint8=np.uint8,
                float32=np.float32, int32=np.int32, Path=Path,
                RuleImageMallResourceMixin=object, cached_property=cached_property,
                logger=LOG, get_current_humanizer=lambda: None)
definitions('module/atom/image.py', image_ns, {'RuleImage'})
RuleImage = image_ns['RuleImage']

ocr_ns = dict(Enum=Enum, Any=object, np=np, cv2=cv2, time=time,
              cached_property=cached_property, BoxedResult=object, logger=LOG,
              re=re, float2str=str, timedelta=timedelta,
              get_current_humanizer=lambda: None)
definitions('module/ocr/base_ocr.py', ocr_ns)
definitions('module/ocr/sub_ocr.py', ocr_ns)
definitions('module/atom/ocr.py', ocr_ns, {'RuleOcr'})
RuleOcr = ocr_ns['RuleOcr']

# Real image resources and the two retained small OCR rules. Other action/OCR
# resources are replaced with sentinels or mocks; no input API is loaded.
asset_tree = ast.parse((ROOT / 'tasks/SixRealms/assets.py').read_text(encoding='utf-8'))
asset_class = next(node for node in asset_tree.body if isinstance(node, ast.ClassDef))
asset_class.body = [node for node in asset_class.body if isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and (node.value.func.id == 'RuleImage'
                         or node.targets[0].id in {'O_TWO_TECHNIQUE_LEVEL', 'O_BOSS_ROUNDS_REMAINING'})]
asset_ns = dict(RuleImage=RuleImage, RuleOcr=RuleOcr)
exec(compile(ast.Module(body=[asset_class], type_ignores=[]), 'actual_six_assets', 'exec'), asset_ns)
A = asset_ns['SixRealmsAssets']
for relative in ('tasks/GlobalGame/assets.py', 'tasks/GameUi/assets.py'):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    cls.body = [node for node in cls.body if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == 'RuleImage']
    ns = dict(RuleImage=RuleImage)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), relative, 'exec'), ns)
    for name, rule in vars(ns[cls.name]).items():
        if isinstance(rule, RuleImage) and not hasattr(A, name):
            setattr(A, name, rule)
for rule in vars(A).values():
    if isinstance(rule, RuleImage):
        rule.file = str(ROOT / rule.file)
for name in ('C_UI_REWARD', 'C_STORE_ANIMATE_KEEP'):
    setattr(A, name, name)


class Timer:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class TaskEnd(Exception):
    pass


class ScriptError(Exception):
    pass


class MoonSeaType(Enum):
    island101 = 101
    island102 = 102
    island103 = 103
    island104 = 104
    island105 = 105
    island106 = 106


NS = dict(BaseTask=type('BaseTask', (), {}), SixRealmsAssets=A, logger=LOG, Timer=Timer,
          cached_property=cached_property, re=re, json=json, Path=Path, uuid4=uuid4,
          Callable=Callable, time=time, datetime=datetime, timedelta=timedelta,
          MoonSeaType=MoonSeaType, TaskEnd=TaskEnd, ScriptError=ScriptError,
          __file__=str(ROOT / 'tasks/SixRealms/moon_sea/skills.py'))
skill_tree = ast.parse((ROOT / 'tasks/SixRealms/moon_sea/skills.py').read_text(encoding='utf-8'))
nodes = [node for node in skill_tree.body if isinstance(node, ast.ClassDef)
         or isinstance(node, ast.Assign) and node.targets[0].id == 'ATTRIBUTE_LAYOUTS']
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual_six_skills', 'exec'), NS)
Skills = NS['MoonSeaSkills']
definitions('tasks/SixRealms/moon_sea/map.py', NS)
definitions('tasks/SixRealms/moon_sea/l101.py', NS)
Map, Shop = NS['MoonSeaMap'], NS['MoonSeaL101']
for number in (102, 103, 104, 105):
    NS[f'MoonSeaL{number}'] = type(f'MoonSeaL{number}', (), {})
moon_tree = ast.parse((ROOT / 'tasks/SixRealms/moon_sea/moon_sea.py').read_text(encoding='utf-8'))
nodes = [node for node in moon_tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
         or isinstance(node, ast.Assign) and node.targets[0].id in {
             'JUE_SAFE_POINTS', 'JUE_TARGET_RULE', 'JUE_MARK_CONFIRM_RULE', 'BOSS_REAL_SCENE_RULES'}]
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual_six_moon', 'exec'), NS)
Moon = NS['MoonSea']
NS.update(SwitchSoul=type('SwitchSoul', (), {}), GameUi=type('GameUi', (), {}),
          page_shikigami_records=object(), page_six_gates=object())
definitions('tasks/SixRealms/script_task.py', NS, {'ScriptTask'})
Script = NS['ScriptTask']


def pixels(name):
    return np.array(Image.open(FIXTURES / f'{name}.png').convert('RGB'))


def task(directory, cls=Moon):
    obj = cls()
    obj.device = SimpleNamespace(image=np.zeros((720, 1280, 3), dtype=np.uint8),
                                 click=Mock(), sleep=Mock(), save_screenshot=Mock(),
                                 stuck_record_clear=Mock(), stuck_record_add=Mock())
    obj.six_flags_path = Path(directory) / 'flags.json'
    obj.config = SimpleNamespace(config_name='offline', generation='test',
        model=SimpleNamespace(six_realms=SimpleNamespace(
            six_realms_gate=SimpleNamespace(power_enhance_level=4, number_enable=False),
            switch_soul_config=SimpleNamespace(enable=True, switch_group_team='test'))))
    obj.screenshot = Mock()
    obj.visible = set()
    obj.appear = Mock(side_effect=lambda rule, **kw: rule in obj.visible)
    obj.appear_rgb = obj.appear
    obj.clicks = []
    def click(rule, **kwargs):
        if rule not in obj.visible:
            return False
        obj.clicks.append(rule)
        return True
    obj.appear_then_click = Mock(side_effect=click)
    obj.ui_click_until_disappear = Mock()
    obj.ui_click = Mock()
    obj.wait_until_stable = Mock()
    obj.wait_until_appear = Mock(return_value=True)
    obj.wait_animate_stable = Mock()
    obj.O_COIN_NUM = SimpleNamespace(ocr=Mock(return_value=500))
    obj.O_SKILL_REFRESH = SimpleNamespace(ocr=Mock(return_value='剩3次'))
    obj.O_STORE_REFRESH_TIME = SimpleNamespace(ocr=Mock(return_value='剩3次'))
    obj.O_SIXREALMS_NUMBER = SimpleNamespace(ocr=Mock(return_value=10))
    obj.push_notify = Mock()
    obj.load_run_flags(new_run=True)
    return obj


class OfflineBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='six_simplified_')
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.obj = task(self.temp)
        LOG.reset_mock()

    def panel(self, name):
        titles, buttons = NS['ATTRIBUTE_LAYOUTS'][name]
        self.obj.visible = {*titles, *buttons}

    def real_pixels(self, fixture):
        self.obj.device.image = pixels(fixture)
        self.obj.appear = Mock(side_effect=lambda rule, **kw:
                               isinstance(rule, RuleImage) and rule.match(self.obj.device.image))
        self.obj.appear_rgb = self.obj.appear


class AttributeTests(OfflineBase):
    def test_three_always_strength_without_exact_power(self):
        self.panel('THREE')
        self.obj.cnt_skillpower = 20
        self.assertTrue(self.obj.select_enhancement())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_THREE_STRENGTH])
        self.assertEqual(self.obj.cnt_skillpower, 21)
        self.obj.device.sleep.assert_not_called()
        self.obj.screenshot.assert_not_called()

    def test_three_dispatch_failure_does_not_increment(self):
        self.panel('THREE')
        self.obj.appear_then_click.return_value = False
        self.obj.appear_then_click.side_effect = None
        self.assertTrue(self.obj.select_enhancement())
        self.assertEqual(self.obj.cnt_skillpower, 1)

    def test_two_level1_selects_technique(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=1)
        self.assertTrue(self.obj.select_enhancement())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_TECHNIQUE])
        self.assertFalse(self.obj.charm_selected)

    def test_two_level2_selects_technique(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=2)
        self.assertTrue(self.obj.select_enhancement())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_TECHNIQUE])

    def test_two_level3_selects_charm_and_saves_flag(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=3)
        self.assertTrue(self.obj.select_enhancement())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_CHARM])
        self.assertTrue(json.loads(self.obj.six_flags_path.read_text())['charm_selected'])

    def test_two_level_above3_selects_charm(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=8)
        self.obj.select_enhancement()
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_CHARM])

    def test_two_unknown_one_retry_then_no_blind_click(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=None)
        with self.assertRaisesRegex(ScriptError, 'no selection sent'):
            self.obj.select_enhancement()
        self.assertEqual(self.obj.read_technique_level.call_count, 2)
        self.obj.screenshot.assert_called_once()
        self.assertFalse(self.obj.clicks)
        self.assertFalse(self.obj.charm_selected)
        self.obj.device.save_screenshot.assert_called_once()

    def test_unknown_retry_recovers_level(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(side_effect=[None, 2])
        self.obj.select_enhancement()
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_TECHNIQUE])

    def test_retry_panel_changed_returns_outer(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(return_value=None)
        self.obj.screenshot.side_effect = lambda: self.panel('THREE')
        self.assertTrue(self.obj.select_enhancement())
        self.assertFalse(self.obj.clicks)
        self.obj.read_technique_level.assert_called_once()

    def test_two_requires_identity_not_only_buttons(self):
        self.obj.visible = set(NS['ATTRIBUTE_LAYOUTS']['TWO'][1])
        self.assertIsNone(self.obj.detect_enhancement_layout())
        self.assertFalse(self.obj.select_enhancement())

    def test_two_requires_both_buttons(self):
        self.panel('TWO')
        self.obj.visible.remove(A.I_ATTRIBUTE_TWO_CHARM)
        self.assertIsNone(self.obj.detect_enhancement_layout())

    def test_ordinary_page_not_attribute(self):
        self.obj.visible = {A.I_SKILL_REFRESH, A.I_SELECT_3}
        self.assertIsNone(self.obj.detect_enhancement_layout())

    def test_real_three_and_two_layouts(self):
        for name, expected in [('three_a', 'THREE'), ('three_b', 'THREE'),
                               ('three_attribute_full_scene_real', 'THREE'),
                               ('two', 'TWO'), ('two_attribute_full_scene_real', 'TWO')]:
            with self.subTest(fixture=name):
                self.real_pixels(name)
                self.assertEqual(self.obj.detect_enhancement_layout(), expected)

    def test_same_three_repeated_dispatches_return_to_outer_without_proof(self):
        self.panel('THREE')
        for _ in range(5):
            self.assertTrue(self.obj.select_skill(refresh=True))
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_THREE_STRENGTH] * 5)
        self.obj.device.sleep.assert_not_called()
        self.obj.ui_click_until_disappear.assert_not_called()

    def test_two_to_two_rechecks_real_level_every_page(self):
        self.panel('TWO')
        self.obj.read_technique_level = Mock(side_effect=[1, 2, 3, 3])
        for _ in range(4):
            self.assertTrue(self.obj.select_skill())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_TWO_TECHNIQUE] * 2
                         + [A.I_ATTRIBUTE_TWO_CHARM] * 2)

    def test_actual_outer_consumes_unbounded_attribute_pages(self):
        self.obj._start = Mock(return_value=True)
        self.panel('THREE')
        count = 0
        def send(rule, **kwargs):
            nonlocal count
            if rule not in self.obj.visible:
                return False
            self.obj.clicks.append(rule)
            count += 1
            if count >= 4:
                self.obj.visible = set()
            return True
        self.obj.appear_then_click.side_effect = send
        self.obj.maybe_honglei_guarantee = Mock(return_value=False)
        self.obj.enter_island = Mock(return_value=False)
        self.obj.island_name = Mock(return_value=MoonSeaType.island106)
        self.obj.boss_team_lock = Mock()
        self.obj.boss_battle = Mock(return_value=True)
        self.assertTrue(self.obj.one())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_THREE_STRENGTH] * 4)
        self.obj.boss_battle.assert_called_once()

    def test_generic_popup_only_uses_identity_and_existing_close_action(self):
        self.obj.visible = {A.I_SIX_ATTRIBUTE_REWARD_TITLE, A.I_SIX_ATTRIBUTE_REWARD_CLOSE_HINT}
        self.assertTrue(self.obj.select_skill())
        self.obj.appear_then_click.assert_called_once_with(
            A.I_SIX_ATTRIBUTE_REWARD_TITLE, action=A.C_UI_REWARD, interval=1)
        self.assertFalse(self.obj.honglei_owned)

    def test_real_popup_detects_even_when_reward_contents_change(self):
        self.real_pixels('attribute_reward_popup_real')
        self.assertTrue(self.obj.enhancement_reward_popup_visible())
        # Explicitly synthetic replacement of changing reward contents.
        self.obj.device.image[230:630, 150:1130] = np.random.default_rng(17).integers(
            0, 256, size=(400, 980, 3), dtype=np.uint8)
        self.assertTrue(self.obj.enhancement_reward_popup_visible())
        self.assertIsNone(self.obj.detect_enhancement_layout())

    def test_real_two_level2_layout_is_usable(self):
        self.real_pixels('two_technique_level_2_real')
        self.assertEqual(self.obj.detect_enhancement_layout(), 'TWO')

    def test_cross_layout_buttons_without_title_rejected(self):
        self.real_pixels('two_attribute_full_scene_real')
        for name in ('THREE', 'TWO'):
            for rule in NS['ATTRIBUTE_LAYOUTS'][name][0]:
                x, y, width, height = rule.roi_back
                self.obj.device.image[y:y + height, x:x + width] = 0
        self.assertIsNone(self.obj.detect_enhancement_layout())

    def test_real_two_missing_either_slot_rejected(self):
        for rule in NS['ATTRIBUTE_LAYOUTS']['TWO'][1]:
            with self.subTest(slot=str(rule)):
                self.real_pixels('two_attribute_full_scene_real')
                x, y, width, height = rule.roi_back
                self.obj.device.image[y:y + height, x:x + width] = 0
                self.assertIsNone(self.obj.detect_enhancement_layout())

    def test_mixed_three_two_pages_use_current_layout_each_time(self):
        self.obj.read_technique_level = Mock(side_effect=[2, 3])
        for panel in ('THREE', 'TWO', 'THREE', 'TWO'):
            self.panel(panel)
            self.assertTrue(self.obj.select_skill())
        self.assertEqual(self.obj.clicks, [A.I_ATTRIBUTE_THREE_STRENGTH, A.I_ATTRIBUTE_TWO_TECHNIQUE,
                                        A.I_ATTRIBUTE_THREE_STRENGTH, A.I_ATTRIBUTE_TWO_CHARM])


class FlagsAndEntryTests(OfflineBase):
    def test_flags_only_contain_three_booleans(self):
        self.assertEqual(json.loads(self.obj.six_flags_path.read_text()),
                         dict(honglei_owned=False, charm_selected=False, honglei_guarantee_attempted=False))

    def test_flags_round_trip_and_no_power_progress_persistence(self):
        self.obj.honglei_owned = self.obj.charm_selected = self.obj.honglei_guarantee_attempted = True
        self.obj.cnt_skillpower = 9
        self.obj.save_run_flags()
        other = task(self.temp / 'other')
        other.six_flags_path = self.obj.six_flags_path
        other.load_run_flags()
        self.assertTrue(other.has_honglei and other.charm_selected and other.honglei_guarantee_attempted)
        self.assertEqual((other.cnt_skill101, other.cnt_skillpower), (1, 1))

    def test_new_start_resets_flags(self):
        self.obj.honglei_owned = self.obj.charm_selected = self.obj.honglei_guarantee_attempted = True
        self.obj.save_run_flags()
        self.obj.load_run_flags(new_run=True)
        self.assertFalse(self.obj.honglei_owned or self.obj.charm_selected or self.obj.honglei_guarantee_attempted)

    def test_missing_flags_never_block_continue(self):
        self.obj.six_flags_path.unlink()
        self.assertTrue(self.obj.load_run_flags())

    def test_corrupt_old_flags_do_not_block_continue(self):
        self.obj.six_flags_path.write_text('{bad')
        self.assertTrue(self.obj.load_run_flags())
        self.assertFalse(self.obj.has_honglei)

    def test_non_boolean_values_not_trusted(self):
        self.obj.six_flags_path.write_text(json.dumps(dict(honglei_owned='true', charm_selected=1)))
        self.obj.load_run_flags()
        self.assertFalse(self.obj.has_honglei or self.obj.charm_selected)

    def test_no_state_api_is_required_for_active_scene(self):
        self.obj.visible = {A.I_M_STORE}
        self.assertFalse(hasattr(self.obj, 'six_state_store'))
        self.assertTrue(self.obj._start())

    def test_positive_ownership_is_idempotent(self):
        self.obj.save_run_flags = Mock(return_value=True)
        self.obj.confirm_honglei_owned('battle')
        self.obj.confirm_honglei_owned('battle_again')
        self.obj.save_run_flags.assert_called_once()
        self.assertEqual(self.obj.cnt_skill101, 1)

    def test_legacy_pending_journal_is_not_read(self):
        journal = self.temp / 'experimental.json'
        journal.write_text(json.dumps({'state': {'pending_selection': {'click_sent': True}}}))
        before = journal.read_bytes()
        self.obj.visible = {A.I_M_STORE}
        self.assertTrue(self.obj._start())
        self.assertEqual(journal.read_bytes(), before)

    def test_continue_loads_sticky_flags(self):
        self.obj.charm_selected = True
        self.obj.save_run_flags()
        self.obj.visible = {A.I_MCONINUE}
        self.assertTrue(self.obj._start())
        self.assertTrue(self.obj.charm_selected)
        self.obj.ui_click_until_disappear.assert_called_once_with(A.I_MCONINUE)

    def test_start_creates_fresh_flags_without_old_journal_gate(self):
        self.obj.charm_selected = self.obj.honglei_owned = True
        self.obj.save_run_flags()
        frames = [{A.I_MSTART}, {A.I_MSHOUZU}, {A.I_PREPARE_BATTLE}, {A.I_M_STORE}]
        self.obj.screenshot.side_effect = lambda: setattr(self.obj, 'visible', frames.pop(0))
        self.assertTrue(self.obj._start())
        self.assertFalse(self.obj.charm_selected or self.obj.honglei_owned)

    def test_known_in_run_pages_enter_immediately(self):
        scenes = [{A.I_M_STORE}, set(NS['ATTRIBUTE_LAYOUTS']['THREE'][0] + NS['ATTRIBUTE_LAYOUTS']['THREE'][1]),
                  {A.I_SIX_ATTRIBUTE_REWARD_TITLE, A.I_SIX_ATTRIBUTE_REWARD_CLOSE_HINT},
                  {A.I_SKILL_REFRESH, A.I_SELECT_3}, {A.I_STORE_EXIT},
                  {A.I_SIX_BOSS_NAME, A.I_SIX_BOSS_AUTO},
                  {A.I_BATTLE_SKILL101, A.I_SIX_BOSS_AUTO}]
        for scene in scenes:
            with self.subTest(scene=[str(rule) for rule in scene]):
                self.obj.visible = scene
                self.assertTrue(self.obj._start())

    def test_script_bypasses_global_navigation_when_already_inside(self):
        obj = task(self.temp / 'script', Script)
        obj.visible = {A.I_M_STORE, A.I_CHECK_MAIN}
        obj.ui_get_current_page = Mock()
        obj.ui_goto_page = Mock()
        obj.ui_goto = Mock()
        obj.run_switch_soul = Mock()
        obj.run_moon_sea = Mock()
        obj.set_next_run = Mock()
        with self.assertRaises(TaskEnd):
            obj.run()
        obj.run_moon_sea.assert_called_once()
        obj.ui_get_current_page.assert_not_called()
        obj.run_switch_soul.assert_not_called()

    def test_direct_boss_battle_resumes_without_waiting_for_island_fire(self):
        self.obj.visible = set(NS['BOSS_REAL_SCENE_RULES'])
        self.obj.boss_battle = Mock(return_value=True)
        self.obj.island_name = Mock()
        self.obj.boss_team_lock = Mock()
        self.assertTrue(self.obj.one())
        self.obj.boss_battle.assert_called_once()
        self.obj.island_name.assert_not_called()
        self.obj.boss_team_lock.assert_not_called()

    def test_direct_ordinary_battle_resumes_without_waiting_for_npc_fire(self):
        self.obj.visible = {A.I_BATTLE_SKILL101, A.I_SIX_BOSS_AUTO}
        self.obj.island_battle = Mock(side_effect=lambda: setattr(self.obj, 'visible',
                                                                 set(NS['BOSS_REAL_SCENE_RULES'])))
        self.obj.boss_battle = Mock(return_value=True)
        self.obj.island_name = Mock()
        self.assertTrue(self.obj.one())
        self.obj.island_battle.assert_called_once()
        self.obj.boss_battle.assert_called_once()
        self.obj.island_name.assert_not_called()


class OrdinarySkillsAndRouteTests(OfflineBase):
    def ordinary(self, candidate=False):
        self.obj.visible = {A.I_SKILL_REFRESH, A.I_SELECT_3}
        if candidate:
            self.obj.visible.add(A.I_SKILL101)
        original = tuple(A.I_SKILL101.roi_front)
        self.addCleanup(setattr, A.I_SKILL101, 'roi_front', list(original))
        A.I_SKILL101.roi_front[0] = 400
        self.obj.visible.add(A.I_SELECT_1)
        self.obj.appear_rgb = Mock(return_value=True)
        def dispatch(rule, **kwargs):
            self.obj.clicks.append(rule)
            if rule in self.obj.selects_button:
                self.obj.appear_rgb.return_value = False
            return True
        self.obj.appear_then_click.side_effect = dispatch

    def test_natural_honglei_selected_without_refresh_regardless_countdown(self):
        self.ordinary(candidate=True)
        self.obj.boss_rounds_remaining = 19
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SELECT_1])
        self.assertTrue(self.obj.has_honglei)

    def test_honglei_in_fourth_column_is_not_treated_as_fallback(self):
        self.ordinary(candidate=True)
        A.I_SKILL101.roi_front[0] = 1000
        self.obj.cnt_skillpower = 4
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SELECT_3])
        self.assertTrue(self.obj.has_honglei)

    def test_other_skill_does_not_mark_honglei(self):
        self.ordinary()
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SELECT_3])
        self.assertFalse(self.obj.has_honglei)

    def test_original_refresh_waits_until_power_proxy_target(self):
        self.ordinary()
        self.obj.cnt_skillpower = 3
        self.obj.select_skill(refresh=True)
        self.assertNotIn(A.I_SKILL_REFRESH, self.obj.clicks)

    def test_original_refresh_not_gated_by_remaining_and_can_use_multiple_refreshes(self):
        self.ordinary()
        self.obj.cnt_skillpower = 4
        self.obj.boss_rounds_remaining = 19
        counts = iter(['剩2次', '剩1次', '剩0次'])
        self.obj.O_SKILL_REFRESH.ocr.side_effect = lambda image: next(counts)
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SKILL_REFRESH, A.I_SKILL_REFRESH, A.I_SELECT_3])

    def test_refresh_protected_by_gold_remaining_and_owned(self):
        for mode in ('poor', 'none_left', 'owned', 'disabled'):
            with self.subTest(mode=mode):
                self.obj = task(self.temp / mode)
                self.ordinary()
                self.obj.cnt_skillpower = 4
                if mode == 'poor':
                    self.obj.O_COIN_NUM.ocr.return_value = 49
                elif mode == 'none_left':
                    self.obj.O_SKILL_REFRESH.ocr.return_value = '剩0次'
                elif mode == 'owned':
                    self.obj.confirm_honglei_owned('test')
                self.obj.select_skill(refresh=mode != 'disabled')
                self.assertNotIn(A.I_SKILL_REFRESH, self.obj.clicks)

    def test_refresh_stops_as_soon_as_honglei_appears(self):
        self.ordinary()
        self.obj.cnt_skillpower = 4
        def stable(*args, **kwargs):
            self.obj.visible.add(A.I_SKILL101)
        self.obj.wait_until_stable.side_effect = stable
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SKILL_REFRESH, A.I_SELECT_1])
        self.assertTrue(self.obj.has_honglei)

    def test_refresh_unreadable_remaining_is_not_spent(self):
        self.ordinary()
        self.obj.cnt_skillpower = 4
        self.obj.O_SKILL_REFRESH.ocr.return_value = 'unreadable'
        self.obj.select_skill(refresh=True)
        self.assertEqual(self.obj.clicks, [A.I_SELECT_3])

    def test_baseline_route_early_and_late_orders(self):
        for owned, power, expected in [(False, 1, [A.I_UI_CANCEL, A.I_ZHAN, A.I_SHENMI, A.I_NINGXI, A.I_XING, A.I_HUNDUN]),
                                       (True, 1, [A.I_UI_CANCEL, A.I_SHENMI, A.I_NINGXI, A.I_ZHAN, A.I_XING, A.I_HUNDUN]),
                                       (False, 4, [A.I_UI_CANCEL, A.I_SHENMI, A.I_NINGXI, A.I_ZHAN, A.I_XING, A.I_HUNDUN])]:
            with self.subTest(owned=owned, power=power):
                self.obj.cnt_skill101, self.obj.cnt_skillpower = int(owned), power
                self.obj.appear_then_click = Mock(return_value=False)
                self.obj.enter_island()
                self.assertEqual([entry.args[0] for entry in self.obj.appear_then_click.call_args_list], expected)

    def test_insufficient_ninngxi_enter_preserved(self):
        self.obj.visible = {A.I_NINGXI_INSUFFICIENT_ENTER}
        self.assertTrue(self.obj.enter_island())
        self.obj.ui_click_until_disappear.assert_called_once_with(A.I_NINGXI_INSUFFICIENT_ENTER, interval=1)
        self.obj.appear_then_click.assert_not_called()

    def test_battle_positive_evidence_is_sticky(self):
        self.obj.visible = {A.I_SIX_BOSS_AUTO, A.I_BATTLE_SKILL101}
        self.assertTrue(self.obj.observe_honglei_in_battle())
        self.obj.visible = set()
        self.assertFalse(self.obj.observe_honglei_in_battle())
        self.assertTrue(self.obj.has_honglei)

    def test_unobservable_skill_bar_cannot_create_or_clear_owned(self):
        self.obj.visible = {A.I_BATTLE_SKILL101}
        self.assertFalse(self.obj.observe_honglei_in_battle())
        self.assertFalse(self.obj.has_honglei)
        self.obj.confirm_honglei_owned('test')
        self.obj.visible = set()
        self.obj.observe_honglei_in_battle()
        self.assertTrue(self.obj.has_honglei)

    def test_shop_purchase_success_confirms_owned(self):
        self.obj.visible = {A.I_UI_CONFIRM}
        self.assertTrue(self.obj.buy_skill_101())
        self.assertTrue(self.obj.has_honglei)

    def test_shop_failed_confirmation_does_not_confirm_owned(self):
        self.obj.visible = {A.I_UI_CONFIRM}
        self.obj.wait_until_appear.side_effect = [True, False]
        self.assertFalse(self.obj.buy_skill_101())
        self.assertFalse(self.obj.has_honglei)


class GuaranteeTests(OfflineBase):
    def prepare(self, remaining=2):
        self.obj.map_scene_ready = Mock(return_value=True)
        self.obj.read_boss_rounds_remaining = Mock(return_value=remaining)
        self.obj.visible = {A.I_M_STORE_ACTIVITY}
        self.obj.run_l101 = Mock()
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def normal_shop_sequence(self, purchase=True):
        frames = [{A.I_SIX_SUMMON_CONFIRM_TITLE, A.I_UI_CONFIRM},
                  {A.I_NINGXI}, {A.I_STORE_EXIT}]
        self.obj.screenshot.side_effect = lambda: setattr(self.obj, 'visible', frames.pop(0))
        if purchase:
            self.obj.run_l101.side_effect = lambda: self.obj.confirm_honglei_owned('offline_store')

    def test_remaining3_does_not_summon(self):
        self.obj.map_scene_ready = Mock(return_value=True)
        self.obj.read_boss_rounds_remaining = Mock(return_value=3)
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def test_remaining2_summons_once_and_reuses_store(self):
        self.prepare(2)
        self.normal_shop_sequence()
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertEqual(self.obj.clicks, [A.I_M_STORE_ACTIVITY, A.I_UI_CONFIRM, A.I_NINGXI])
        self.obj.run_l101.assert_called_once()
        self.assertTrue(self.obj.has_honglei)
        self.assertFalse(self.obj.maybe_honglei_guarantee())

    def test_remaining1_summons_once(self):
        self.prepare(1)
        self.normal_shop_sequence()
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertTrue(self.obj.honglei_guarantee_attempted)

    def test_owned_skips_summon(self):
        self.obj.confirm_honglei_owned('battle')
        self.obj.read_boss_rounds_remaining = Mock(return_value=1)
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.read_boss_rounds_remaining.assert_not_called()

    def test_attempted_skips_repeat_after_resume(self):
        self.obj.honglei_guarantee_attempted = True
        self.obj.save_run_flags()
        self.obj.load_run_flags()
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def test_unreadable_remaining_never_guessed_from_counter(self):
        self.obj.map_scene_ready = Mock(return_value=True)
        self.obj.read_boss_rounds_remaining = Mock(return_value=None)
        self.obj.completed_cell_count = 18
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def test_single_small_digit_noise_does_not_summon(self):
        self.obj.map_scene_ready = Mock(return_value=True)
        self.obj.read_boss_rounds_remaining = Mock(side_effect=[2, 3, 2])
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def test_insufficient_dialog_reuses_right_enter_and_store(self):
        self.prepare()
        frames = [{A.I_SIX_SUMMON_INSUFFICIENT_TITLE, A.I_NINGXI_INSUFFICIENT_ENTER},
                  {A.I_STORE_EXIT}]
        self.obj.screenshot.side_effect = lambda: setattr(self.obj, 'visible', frames.pop(0))
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertEqual(self.obj.clicks, [A.I_M_STORE_ACTIVITY, A.I_NINGXI_INSUFFICIENT_ENTER])
        self.obj.run_l101.assert_called_once()

    def test_store_without_honglei_warns_and_returns_outer(self):
        self.prepare()
        self.normal_shop_sequence(purchase=False)
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertFalse(self.obj.has_honglei)
        self.assertTrue(self.obj.honglei_guarantee_attempted)
        LOG.warning.assert_called()

    def test_unobserved_outcome_does_not_kill_run_or_resummon(self):
        self.prepare()
        self.obj.screenshot.side_effect = lambda: setattr(self.obj, 'visible', set())
        self.assertTrue(self.obj.maybe_honglei_guarantee())
        self.assertEqual(self.obj.clicks, [A.I_M_STORE_ACTIVITY])
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.device.sleep.assert_not_called()

    def test_save_failure_prevents_resource_consumption(self):
        self.prepare()
        self.obj.save_run_flags = Mock(return_value=False)
        self.assertFalse(self.obj.maybe_honglei_guarantee())
        self.obj.appear_then_click.assert_not_called()

    def test_modal_scene_not_valid_map(self):
        self.obj.visible = {A.I_M_STORE_ACTIVITY, A.I_SIX_SUMMON_CONFIRM_TITLE}
        self.assertFalse(self.obj.map_scene_ready())
        self.assertFalse(self.obj.maybe_honglei_guarantee())

    def test_real_summon_map_and_dialog_identities(self):
        self.real_pixels('summon_map_real')
        self.assertTrue(self.obj.map_scene_ready())
        self.real_pixels('summon_confirm_real')
        self.assertTrue(self.obj.appear(A.I_SIX_SUMMON_CONFIRM_TITLE))
        self.assertFalse(self.obj.map_scene_ready())
        self.real_pixels('summon_insufficient_real')
        self.assertTrue(self.obj.appear(A.I_SIX_SUMMON_INSUFFICIENT_TITLE))
        self.assertTrue(self.obj.appear(A.I_NINGXI_INSUFFICIENT_ENTER))
        self.assertFalse(self.obj.map_scene_ready())

    def test_final_guarantee_changes_no_route_power(self):
        self.prepare()
        before = self.obj.cnt_skillpower
        self.normal_shop_sequence()
        self.obj.maybe_honglei_guarantee()
        self.assertEqual(self.obj.cnt_skillpower, before)


class GreenMarkTests(OfflineBase):
    def test_exact_manually_validated_points(self):
        self.assertEqual(NS['JUE_SAFE_POINTS'], ((625, 544), (624, 526), (631, 569)))

    def test_charm_false_has_no_extra_inputs(self):
        self.obj.maybe_mark_jue_in_boss()
        self.obj.screenshot.assert_not_called()
        self.obj.device.click.assert_not_called()

    def test_mark_already_present_has_zero_clicks(self):
        click = Mock()
        self.assertEqual(NS['mark_with_safe_points'](NS['JUE_SAFE_POINTS'], lambda: True,
                                                    lambda: True, click), ('already_marked', 0))
        click.assert_not_called()

    def test_p1_p2_p3_stop_at_first_success(self):
        for success in (1, 2, 3):
            with self.subTest(success=success):
                click = Mock()
                verdicts = iter([False] * success + [True])
                self.assertEqual(NS['mark_with_safe_points'](NS['JUE_SAFE_POINTS'], lambda: True,
                                                            lambda: next(verdicts), click), ('marked', success))
                self.assertEqual(click.call_args_list, [call(*point) for point in NS['JUE_SAFE_POINTS'][:success]])

    def test_all_fail_stops_after_three(self):
        click = Mock()
        self.assertEqual(NS['mark_with_safe_points'](NS['JUE_SAFE_POINTS'], lambda: True,
                                                    lambda: False, click), ('failed', 3))
        self.assertEqual(click.call_count, 3)

    def test_target_missing_has_zero_clicks(self):
        click = Mock()
        self.assertEqual(NS['mark_with_safe_points'](NS['JUE_SAFE_POINTS'], lambda: False,
                                                    lambda: False, click), ('target_missing', 0))
        click.assert_not_called()

    def test_real_positive_and_negative_are_target_specific(self):
        for fixture, expected in [('boss_a', True), ('boss_b', True), ('boss_unmarked', False)]:
            with self.subTest(fixture=fixture):
                self.real_pixels(fixture)
                self.assertTrue(self.obj.six_real_boss_scene())
                self.assertTrue(self.obj.six_jue_target_present())
                self.assertEqual(self.obj.appear(NS['JUE_MARK_CONFIRM_RULE']), expected)

    def test_real_already_marked_charm_has_no_clicks(self):
        self.real_pixels('boss_a')
        self.obj.charm_selected = True
        self.obj.maybe_mark_jue_in_boss()
        self.obj.device.click.assert_not_called()
        self.assertTrue(self.obj._six_jue_mark_checked)

    def test_mark_confirms_three_consecutive_target_frames_after_click(self):
        self.obj.charm_selected = True
        self.obj.visible = set(NS['BOSS_REAL_SCENE_RULES']) | {A.I_JUE_TARGET_UNMARKED}
        self.obj.device.click.side_effect = lambda x, y: self.obj.visible.add(A.I_JUE_MARK)
        self.obj.maybe_mark_jue_in_boss()
        self.obj.device.click.assert_called_once_with(625, 544)

    def test_mark_missing_target_keeps_opportunity_for_later_boss_frame(self):
        self.obj.charm_selected = True
        self.obj.visible = set(NS['BOSS_REAL_SCENE_RULES'])
        self.obj.maybe_mark_jue_in_boss()
        self.obj.device.click.assert_not_called()
        self.assertFalse(getattr(self.obj, '_six_jue_mark_checked', False))

    def test_mark_waits_for_real_boss_before_setting_checked(self):
        self.obj.charm_selected = True
        self.obj.visible = {A.I_JUE_TARGET_UNMARKED}
        self.obj.maybe_mark_jue_in_boss()
        self.obj.device.click.assert_not_called()
        self.assertFalse(getattr(self.obj, '_six_jue_mark_checked', False))

    def test_unstable_marker_does_not_satisfy_three_frame_confirmation(self):
        self.obj.charm_selected = True
        base = set(NS['BOSS_REAL_SCENE_RULES']) | {A.I_JUE_TARGET_UNMARKED}
        self.obj.visible = set(base)
        frame = 0
        def screenshot():
            nonlocal frame
            frame += 1
            self.obj.visible = base | ({A.I_JUE_MARK} if frame % 2 else set())
        self.obj.screenshot.side_effect = screenshot
        self.obj.maybe_mark_jue_in_boss()
        self.assertEqual(self.obj.device.click.call_args_list,
                         [call(*point) for point in NS['JUE_SAFE_POINTS']])
        LOG.warning.assert_called()

    def test_unrelated_green_marker_outside_roi_does_not_count(self):
        self.real_pixels('boss_unmarked')
        image = self.obj.device.image
        marker = A.I_JUE_MARK.image
        image[150:150 + marker.shape[0], 900:900 + marker.shape[1]] = marker
        self.assertTrue(self.obj.six_jue_target_present())
        self.assertFalse(self.obj.appear(A.I_JUE_MARK))


class SmallOcrRuleTests(OfflineBase):
    def rule(self, name):
        original = getattr(A, name)
        rule = RuleOcr(roi=tuple(original.roi), area=tuple(original.area), mode='Single',
                       method='Default', keyword='', name='offline')
        rule.model = Mock()
        setattr(self.obj, name, rule)
        return rule

    def test_technique_uses_only_small_number_roi(self):
        rule = self.rule('O_TWO_TECHNIQUE_LEVEL')
        rule.model.ocr_single_line.return_value = ('2', 0.95)
        self.assertEqual(self.obj.read_technique_level(), 2)
        self.assertEqual(tuple(rule.roi), (431, 236, 19, 24))
        self.assertEqual(rule.model.ocr_single_line.call_args.args[0].shape, (24, 19, 3))

    def test_technique_supported_digits_and_bad_ocr(self):
        rule = self.rule('O_TWO_TECHNIQUE_LEVEL')
        for value in (1, 2, 3, 8):
            rule.model.ocr_single_line.return_value = (str(value), 0.95)
            self.assertEqual(self.obj.read_technique_level(), value)
        for text, score in [('', 1), ('I', 1), ('0', 1), ('Lv2', 1), ('2', 0.89)]:
            rule.model.ocr_single_line.return_value = (text, score)
            self.assertIsNone(self.obj.read_technique_level())

    def test_countdown_strict_numbers_and_failures(self):
        rule = self.rule('O_BOSS_ROUNDS_REMAINING')
        for value in (1, 2, 3, 15, 19):
            rule.model.ocr_single_line.return_value = (str(value), 0.95)
            self.assertEqual(self.obj.read_boss_rounds_remaining(), value)
        for text, score in [('', 1), ('I', 1), ('20', 1), ('01', 1), ('1', 0.89), ('1', 1.1)]:
            rule.model.ocr_single_line.return_value = (text, score)
            self.assertIsNone(self.obj.read_boss_rounds_remaining())

    def test_wrong_resolution_never_ocr(self):
        technique = self.rule('O_TWO_TECHNIQUE_LEVEL')
        countdown = self.rule('O_BOSS_ROUNDS_REMAINING')
        self.obj.device.image = np.zeros((360, 640, 3), dtype=np.uint8)
        self.assertIsNone(self.obj.read_technique_level())
        self.assertIsNone(self.obj.read_boss_rounds_remaining())
        technique.model.ocr_single_line.assert_not_called()
        countdown.model.ocr_single_line.assert_not_called()


class RealOcrAndIconTests(OfflineBase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'module/ocr/rapid_ocr.py').read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom)
                     or node.module not in {'module.logger', 'module.ocr.result'}]
        ns = dict(logger=LOG, BoxedResult=object)
        exec(compile(tree, 'offline_existing_cpu_ocr', 'exec'), ns)
        cls.model = ns['RapidOcrModel'](model_dir=str(ROOT / 'toolkit/ocr_models'),
                                      device='cpu', model_type='small', cpu_threads=4)

    def bind_rule(self, name):
        original = getattr(A, name)
        method = 'Default' if name == 'O_TWO_TECHNIQUE_LEVEL' else 'CF_HSV(0F5A64,23FFFF)'
        rule = RuleOcr(roi=tuple(original.roi), area=tuple(original.area), mode='Single',
                       method=method, keyword='', name='offline')
        rule.model = self.model
        setattr(self.obj, name, rule)

    def test_real_technique_level1(self):
        self.bind_rule('O_TWO_TECHNIQUE_LEVEL')
        self.real_pixels('two_attribute_full_scene_real')
        self.assertEqual(self.obj.read_technique_level(), 1)

    def test_real_technique_level2(self):
        self.bind_rule('O_TWO_TECHNIQUE_LEVEL')
        self.real_pixels('two_technique_level_2_real')
        self.assertEqual(self.obj.read_technique_level(), 2)

    @unittest.skip('No genuine TWO technique Lv3 screenshot supplied; logical Lv3 decision is covered separately')
    def test_real_technique_level3_needs_user_fixture(self):
        pass

    def test_real_countdown15_and_erased_number(self):
        self.bind_rule('O_BOSS_ROUNDS_REMAINING')
        for fixture in ('summon_map_real', 'summon_rest_real'):
            self.real_pixels(fixture)
            self.assertEqual(self.obj.read_boss_rounds_remaining(), 15)
        self.obj.device.image[235:259, 1072:1108] = 0
        self.assertIsNone(self.obj.read_boss_rounds_remaining())

    def test_icon_body_with_synthetic_level_overlays_stays_owned(self):
        # Test input variations only: these are not genuine Lv1/Lv2/Lv3 fixtures.
        for label in ('1', '2', '3', '9'):
            with self.subTest(label=label):
                self.obj.honglei_owned = False
                self.obj.cnt_skill101 = 0
                self.real_pixels('boss_a')
                # Genuine level glyph lives left of the icon body (x75..84),
                # overlapping its y span. Alter only that badge glyph ROI.
                self.obj.device.image[86:100, 75:85] = 0
                glyph = np.zeros((14, 10, 3), dtype=np.uint8)
                cv2.putText(glyph, label, (0, 12), cv2.FONT_HERSHEY_SIMPLEX,
                            0.4, (255, 255, 255), 1)
                self.obj.device.image[86:100, 75:85] = glyph
                self.assertTrue(self.obj.observe_honglei_in_battle())
                self.assertTrue(self.obj.has_honglei)

    def test_battle_icon_search_does_not_include_level_badge(self):
        x, y, width, height = A.I_BATTLE_SKILL101.roi_back
        self.assertGreaterEqual(y, 83)
        height, width = A.I_BATTLE_SKILL101.image.shape[:2]
        self.assertLessEqual(height, A.I_BATTLE_SKILL101.roi_back[3])
        self.assertLessEqual(width, A.I_BATTLE_SKILL101.roi_back[2])

    def test_real_different_battle_levels_both_confirm_honglei(self):
        # Actual stored captures show Lv1 (positive) and Lv3 (unmarked frame).
        # This uses no generated labels and does not claim a real Lv2 fixture.
        for fixture in ('boss_a', 'boss_unmarked'):
            with self.subTest(fixture=fixture):
                self.obj.honglei_owned = False
                self.obj.cnt_skill101 = 0
                self.real_pixels(fixture)
                self.assertTrue(self.obj.observe_honglei_in_battle())
                self.assertTrue(self.obj.has_honglei)

    def test_erased_icon_match_never_clears_owned(self):
        self.real_pixels('boss_a')
        self.assertTrue(self.obj.observe_honglei_in_battle())
        x, y, width, height = A.I_BATTLE_SKILL101.roi_back
        self.obj.device.image[y:y + height, x:x + width] = 0
        self.assertFalse(self.obj.observe_honglei_in_battle())
        self.assertTrue(self.obj.has_honglei)


class RemovedComplexityTests(unittest.TestCase):
    def test_formal_flow_has_no_experimental_state_imports_or_methods(self):
        forbidden = {'completed_cell_count', 'bloom_completed_count', 'pending_cell',
                     'pending_selection', 'confirm_enhancement_selection', 'record_transition_probe',
                     'complete_cell_at_map', 'synchronize_map_progress', '_six_run_state'}
        for relative in ('skills.py', 'map.py', 'moon_sea.py', 'l101.py'):
            tree = ast.parse((ROOT / 'tasks/SixRealms/moon_sea' / relative).read_text(encoding='utf-8'))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            self.assertNotIn('tasks.SixRealms.run_state', imports)
            identifiers = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            identifiers |= {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
            self.assertFalse(identifiers & forbidden, (relative, identifiers & forbidden))

    def test_attribute_dispatch_has_no_fixed_wait_or_confirmation_poll(self):
        node = next(node for node in ast.walk(skill_tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'select_enhancement')
        calls = {node.func.attr for node in ast.walk(node)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(calls & {'sleep', 'ui_click_until_disappear', 'commit_selection', 'wait_until_appear'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
