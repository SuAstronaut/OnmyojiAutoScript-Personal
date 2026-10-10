"""Single-player departure regression; no game/device is started.

Boundary sequences prove actions, not visual evidence. Pixel characterizations
include the original 16:42 exit dialog; departure transitions remain simulated.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.Exploration.test_boss_exit import (
    ROOT, BoundaryTask, WorkflowTask, Stuck, pixel_task, methods,
)


BACKS = {'I_BACK_YOLLOW', 'I_UI_BACK_YELLOW', 'I_UI_BACK_BLUE'}


class ExitTask(BoundaryTask):
    def __init__(self, frames):
        super().__init__(frames)
        original_appear = self.appear
        original_scene = self.get_current_scene
        # The historical partial entrance is unknown: only its arrow is assumed
        # visible. This double does not assert what the actual incident showed.
        self.appear = lambda target, **kw: (
            target == 'I_UI_BACK_YELLOW' if self.state == 'partial_entrance'
            else original_appear(target, **kw))
        self.get_current_scene = lambda **kw: (
            self.Scene.MAIN if self.state == 'stale_main' else original_scene(**kw))
        self.seen = []
        self.device = SimpleNamespace(save_screenshot=Mock())
        original_shot = self.screenshot
        def screenshot():
            original_shot()
            self.seen.append(self.shots)
        self.screenshot = screenshot


@pytest.mark.parametrize('skip', [False, True])
@pytest.mark.parametrize('destination', ['entrance', 'world'])
@pytest.mark.parametrize('transition', [[], ['unknown'], ['partial_entrance'] * 12,
                                      ['stale_main'] * 8])
def test_confirm_then_wait_never_clicks_a_second_back(skip, destination, transition):
    task = ExitTask(['map', 'dialog'] + transition + [destination, destination])
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM']
    assert len(task.seen) == len(set(task.seen)) == task.shots
    assert task.shots == 4 + len(transition)


@pytest.mark.parametrize('frames', [
    ['map'], ['map', 'dialog', 'stale_main'],
    ['map', 'dialog', 'partial_entrance'], ['unknown'], ['dialog'],
])
def test_timeout_has_no_blind_back_retry_or_false_success(frames):
    task = ExitTask(frames)
    task.config.script.error.save_error = True
    with pytest.raises(Stuck, match='before confirmed departure'):
        task.quit_explore(safe_solo=True)
    assert len([e for e in task.events if e[0] in BACKS]) <= 1
    assert 20 <= task.clock.now - 1 <= 20.5
    task.device.save_screenshot.assert_called_with(genre='exploration_exit', interval=0)


@pytest.mark.parametrize('destination', ['entrance', 'world'])
def test_already_outside_has_two_new_samples_and_no_action(destination):
    task = ExitTask([destination, destination])
    task.quit_explore(safe_solo=True)
    assert not task.events and task.seen == [1, 2]


def test_unknown_frame_resets_two_frame_confirmation():
    task = ExitTask(['map', 'dialog', 'entrance', 'unknown', 'world', 'entrance', 'entrance'])
    task.quit_explore(safe_solo=True)
    assert task.shots == 7 and len(task.events) == 2


@pytest.mark.parametrize('skip', [False, True])
def test_paper_doll_policy_is_preserved(skip):
    task = ExitTask(['chest', 'map', 'dialog', 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    names = [e[0] for e in task.events]
    assert names.count('I_BATTLE_REWARD') == int(not skip)
    assert names.count('I_BACK_YOLLOW') == names.count('I_E_EXIT_CONFIRM') == 1


def test_each_exit_has_fresh_local_state():
    task = ExitTask(['map', 'dialog', 'entrance', 'entrance'] * 3)
    for _ in range(3):
        task.quit_explore(safe_solo=True)
    assert task.shots == 12
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM'] * 3


def test_normal_shared_exit_remains_on_the_legacy_path():
    # Team and Wanted callers do not opt into safe_solo.
    task = ExitTask(['chest', 'entrance'])
    task._quit_solo_explore = Mock(side_effect=AssertionError('solo path forbidden'))
    task.quit_explore()
    assert [e[0] for e in task.events] == ['I_BATTLE_REWARD']


class NoDarumaTask(WorkflowTask):
    def __init__(self, outside):
        super().__init__()
        self.outside = outside
        self.stage = 'empty'
        self.searches = 0
        self.rounds = 0
        self.world_searches = 0
        self._match_end = SimpleNamespace(stable=lambda *a, **kw: True)
        self.device = SimpleNamespace(image=None)

    def search_up_fight(self):
        self.searches += 1
        return None

    def check_exit(self):
        return self.rounds >= 2

    def appear_then_click(self, target, **kw):
        result = super().appear_then_click(target, **kw)
        if result and target == 'I_E_EXIT_CONFIRM':
            self.state = self.outside
            self.rounds += 1
        return result

    def open_expect_level(self):
        self.world_searches += 1
        self.state = 'entrance'

    def ui_click(self, click, **kw):
        super().ui_click(click, **kw)
        self.stage = 'empty'
        return True


@pytest.mark.parametrize('outside', ['entrance', 'world'])
def test_no_daruma_preserves_exit_policy_and_only_world_reselects(outside):
    task = NoDarumaTask(outside)
    task.run_solo()
    assert task.rounds == 2 and task.searches == 10
    assert task.world_searches == int(outside == 'world')
    assert len([e for e in task.events if e[0] in BACKS]) == 2
    assert task.minions_cnt == task.current_count == 0


@pytest.mark.parametrize('file,expected', [
    ('MuMu-20261010-133758-411.png', 'WORLD'),
    ('MuMu-20261010-133801-620.png', 'ENTRANCE'),
    ('MuMu-20261010-133907-971.png', 'MAIN'),
])
def test_original_pixels_characterize_current_scene_and_return_templates(file, expected):
    path = ROOT.__class__('D:/Documents/MuMu共享文件夹/Screenshots') / file
    task = pixel_task([path])
    assert task.get_current_scene().name == expected
    if expected in ('WORLD', 'ENTRANCE'):
        # The same yellow arrow is present in both outside scenes: it cannot
        # independently authorize another chapter-back action.
        assert task.appear(task.I_BACK_YOLLOW) and task.appear(task.I_UI_BACK_YELLOW)
    else:
        # This manual capture has a status bar over the arrow. Record the actual
        # negative matches rather than fabricating a recognizable MAIN button.
        assert not any(task.appear(getattr(task, name)) for name in BACKS)


@pytest.mark.parametrize('arrived', [False, True])
def test_public_back_main_shortcut_retains_original_behavior(arrived):
    ns = dict(logger=Mock(), page_main='courtyard', Page=object)
    fn = methods('tasks/GameUi/game_ui.py', 'GameUi', {'_try_back_main_shortcut'}, ns)
    task = SimpleNamespace(maybe_screenshot=Mock(), I_BACK_MAIN='BACK_MAIN',
                           appear=Mock(return_value=True), appear_then_click=Mock(return_value=True),
                           ui_wait_until_appear=Mock(return_value=arrived))
    assert fn['_try_back_main_shortcut'](task, False) is arrived
    task.appear_then_click.assert_called_once_with('BACK_MAIN', interval=1.0)
    task.ui_wait_until_appear.assert_called_once_with('courtyard', timeout=5, skip_first_screenshot=False)
    if arrived:
        assert task.ui_current == 'courtyard'


def test_public_navigation_without_overlays_dispatches_unchanged():
    fn = methods('tasks/GameUi/game_ui.py', 'GameUi', {'ui_goto'}, dict(Page=object))['ui_goto']
    task = SimpleNamespace(ui_pages=[SimpleNamespace(overlay=False)], _ui_goto=Mock(return_value=True))
    assert fn(task, 'target', 0.5, False, 9)
    task._ui_goto.assert_called_once_with('target', 0.5, False, 9)


def test_real_entrance_arrow_templates_have_independent_production_cooldowns():
    path = ROOT.__class__('D:/Documents/MuMu共享文件夹/Screenshots') / 'MuMu-20261010-133801-620.png'
    task = pixel_task([path])
    image_type = type(task.I_BACK_YOLLOW)
    ns = dict(RuleImage=image_type, RuleGif=object, RuleOcr=type('UnusedOcr', (), {}),
              Timer=task.quit_explore.__func__.__globals__['Timer'])
    appear = methods('tasks/base_task.py', 'BaseTask', {'appear'}, ns)['appear']
    task.interval_timer = {}
    # Production uses Unix time; an unstarted timer (timestamp zero) is due.
    task.clock.now = 100.0
    assert appear(task, task.I_BACK_YOLLOW, interval=3.5)
    task.clock.sleep(1.666)
    assert not appear(task, task.I_BACK_YOLLOW, interval=3.5)
    assert appear(task, task.I_UI_BACK_YELLOW, interval=3.5)


def test_recorded_incidents_are_the_ordinary_exit_path():
    path = ROOT / 'log/2026-10-10_模拟器.txt'
    if not path.exists():
        pytest.skip('Historical log unavailable')
    lines = path.read_text(encoding='utf-8').splitlines()
    for stamp in ('15:58:51.964', '15:59:36.907', '16:07:23.018'):
        start = next(i for i, line in enumerate(lines) if stamp in line and 'Quit explore' in line)
        block = '\n'.join(lines[start:start + 4])
        assert 'PAGE_BACK_YOLLOW' in block
        assert 'RES_E_EXIT_CONFIRM' in block
        assert 'UI_UI_BACK_YELLOW' in block
        assert 'UI GOTO' not in block


@pytest.mark.parametrize('skip', [False, True])
def test_existing_exit_dialog_is_confirmed_without_requesting_back(skip):
    task = ExitTask(['dialog', 'entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    assert [e[0] for e in task.events] == ['I_E_EXIT_CONFIRM']


@pytest.mark.parametrize('skip', [False, True])
def test_dialog_with_main_flag_outranks_every_settlement_gate(skip):
    task = ExitTask(['map', 'dialog', 'entrance', 'entrance'])
    original_appear = task.appear
    task.appear = lambda target, **kw: (
        True if task.state == 'dialog' and target == 'I_E_MAIN_FLAG'
        else original_appear(target, **kw))
    for name in ('is_in_prepare', 'is_in_battle', 'win_appear', 'reward_grid_appear'):
        original = getattr(task, name)
        def gate(*args, _original=original, **kw):
            assert task.state != 'dialog', 'Dialog must be handled before this gate'
            return _original(*args, **kw)
        setattr(task, name, gate)
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM']


@pytest.mark.parametrize('skip', [False, True])
def test_slow_dialog_and_post_confirmation_animation_wait_without_back_retry(skip):
    task = ExitTask(['map'] + ['stale_main'] * 12 + ['dialog']
                    + ['partial_entrance'] * 8 + ['entrance', 'entrance'])
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    assert [e[0] for e in task.events] == ['I_BACK_YOLLOW', 'I_E_EXIT_CONFIRM']
    assert task.shots == 24


def test_failed_confirm_attempt_waits_for_fresh_dialog_without_backing_again():
    task = ExitTask(['dialog', 'stale_main', 'dialog', 'entrance', 'entrance'])
    original_click = task.appear_then_click
    attempts = []
    def click(target, **kw):
        if target == 'I_E_EXIT_CONFIRM':
            attempts.append(task.shots)
            if len(attempts) == 1:
                return False  # e.g. the existing interval gate has not expired.
        return original_click(target, **kw)
    task.appear_then_click = click
    task.quit_explore(safe_solo=True)
    assert attempts == [1, 3]
    assert [e[0] for e in task.events] == ['I_E_EXIT_CONFIRM']


def test_settlement_wait_emits_blocker_and_progress_then_times_out():
    task = ExitTask(['reward_grid'])
    logger = task.quit_explore.__func__.__globals__['logger']
    with pytest.raises(Stuck):
        task.quit_explore(skip_battle_reward=True)
    messages = [str(c.args[0]) for c in logger.info.call_args_list]
    assert any('phase=wait_settlement:reward_grid' in s for s in messages)
    progress = [s for s in messages if 'exit polling:' in s]
    assert 5 <= len(progress) <= 7
    assert all('polls=' in s and 'elapsed=' in s for s in progress)
    assert not task.events


def test_persistent_dialog_times_out_with_confirm_attempts_but_no_map_back():
    task = ExitTask(['dialog'])
    with pytest.raises(Stuck):
        task.quit_explore(skip_battle_reward=True)
    assert task.events and all(e[0] == 'I_E_EXIT_CONFIRM' for e in task.events)
    assert 20 <= task.clock.now - 1 <= 20.5


ERROR_DIALOG = ROOT / 'log/error/1791621730544/2026-10-10_16-42-10-378349.png'


def dialog_pixel_task(paths):
    """Replay production matchers AND production reward cache/gate methods.

Each replay request invalidates its cache as GeneralBattle.screenshot does.
It is not an emulator capture or evidence of the incident's intermediate frames.
"""
    task = pixel_task(paths)
    from tasks.Component.GeneralBattle import reward_frame
    ns = dict(vars(reward_frame))
    ns.update(logger=Mock(), Timer=task.quit_explore.__func__.__globals__['Timer'])
    for name, fn in methods('tasks/Component/GeneralBattle/general_battle.py', 'GeneralBattle',
                            {'reward_click_actions', 'reward_grid_appear'}, ns).items():
        setattr(task, name, fn.__get__(task, type(task)))
    task.reward_grid_detect_enabled = lambda: True
    task.reward_forbidden = lambda: reward_frame.FORBIDDEN_KEKKAI
    task.reward_hot = lambda: None
    original_shot = task.screenshot
    def screenshot():
        original_shot()
        task._reward_safe_rules = None
        return task.device.image
    task.screenshot = screenshot
    return task


def test_real_error_dialog_matches_confirm_and_false_positive_reward_grid():
    task = dialog_pixel_task([ERROR_DIALOG])
    assert task.appear(task.I_E_EXIT_CONFIRM)
    assert task.appear(task.I_E_MAIN_FLAG) and task.appear(task.I_E_SETTINGS_BUTTON)
    assert not task.is_in_prepare(False) and not task.is_in_battle(False)
    assert not task.win_appear()
    assert not any(task.appear(rule) for rule in (task.I_FALSE, task.I_REWARD,
        task.I_REWARD_GOLD, task.I_EXTRA_INFO, task.I_STATISTICS))
    assert task.reward_grid_appear() and task._reward_grid_found


@pytest.mark.parametrize('skip', [False, True])
def test_real_error_dialog_is_clicked_despite_grid_false_positive(skip):
    from tasks.Exploration.test_boss_exit import MAP_SHOTS
    entrance = ROOT.__class__('D:/Documents/MuMu共享文件夹/Screenshots') / 'MuMu-20261010-133801-620.png'
    task = dialog_pixel_task([MAP_SHOTS[0], ERROR_DIALOG, entrance, entrance])
    # Both frames are real originals, but this sequence is an offline scenario;
    # no actual departure frame after the 16:42 dialog was supplied.
    original_grid = task.reward_grid_appear
    def grid():
        assert task.shots != 2, 'Production grid gate must not starve the real dialog'
        return original_grid()
    task.reward_grid_appear = grid
    task.quit_explore(skip_battle_reward=skip, safe_solo=True)
    assert [e[0] for e in task.events] == [task.I_BACK_YOLLOW.name, task.I_E_EXIT_CONFIRM.name]
    confirm = task.events[1]
    assert confirm[1] == (694, 380, 163, 49)
    assert task.shots == 4
