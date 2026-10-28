"""Desktop 神眸垂世 / 盘长神木·永恒 adapter (1280x720).

Only run_sequence=boss enters this adapter. No public battle/AP assets or
helpers are changed. Runtime evidence and offline validation: SHENMU_BOSS.md.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
import json
import random
import re



SCORE_LIMIT = 35_000_000
REWARD_Y_BAND = (145, 440)  # Full-width exclusion, including frame/glow margins.
SETTLEMENT_RECTS = ((30, 460, 180, 180), (1100, 460, 150, 180))


class BossSafetyStop(RuntimeError):
    def __init__(self, reason, outcome='SAFE_STOP'):
        super().__init__(reason)
        self.outcome = outcome


def parse_number(text):
    """Never repair OCR letters, concatenate lines, or discard punctuation."""
    if not isinstance(text, str) or not re.fullmatch(r'[0-9]{1,10}', text.strip()):
        return None
    return int(text.strip())


def parse_balance(text):
    value = parse_number(text)
    if value is not None:
        return value
    if isinstance(text, str) and re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,2})?万', text.strip()):
        number = text.strip()[:-1]
        places = len(number.split('.')[1]) if '.' in number else 0
        # Display rounding is unknown: subtract one whole display quantum.
        return max(0, int(Decimal(number)*10000 - Decimal(10000)/(10**places)))
    return None


def parse_cost(text):
    if isinstance(text, str) and re.fullmatch(r'[×xX]\s*[1-9][0-9]{0,2}', text.strip()):
        return int(text.strip()[1:].strip())
    return None


def pause(seconds):
    sleep(seconds)


def settlement_point_safe(x, y):
    return (not REWARD_Y_BAND[0] <= y <= REWARD_Y_BAND[1]
            and any(rx <= x < rx+w and ry <= y < ry+h
                    for rx, ry, w, h in SETTLEMENT_RECTS))


def sample_settlement_point(rng=random):
    x, y, w, h = rng.choice(SETTLEMENT_RECTS)
    return rng.randrange(x, x+w), rng.randrange(y, y+h)


class ShenmuBoss:
    ENTRY_TIMEOUT = 25
    UNKNOWN_TIMEOUT = 15
    BATTLE_TIMEOUT = 180  # Safety stop only; never authorizes a battle exit.
    SETTLEMENT_TIMEOUT = 35
    EXIT_TIMEOUT = 20
    EXIT_DIALOG_GRACE = 2  # Known title may arrive before the button animation settles.
    POLL = .25
    SCORE_INTERVAL = 1.5
    LOADING_SCORE_INTERVAL = 2
    SCORE_DIAGNOSTIC_INTERVAL = 15

    def __init__(self, task):
        from module.atom.image import RuleImage
        from module.atom.ocr import RuleOcr
        self.task = task
        self.device = task.device
        folder = Path(__file__).with_name('shenmu_boss')
        manifest = json.loads((folder/'resources.json').read_text(encoding='utf-8'))
        self.rules = {name: RuleImage(roi_front=tuple(spec['roi']),
                         roi_back=tuple(spec.get('search_roi', spec['roi'])), method='Template matching',
                         threshold=spec['threshold'], file=str(folder/(name+'.png')))
                      for name, spec in manifest['templates'].items()}
        self.ocr = {name: RuleOcr(name='SHENMU_'+name, mode='Single', method='Default',
                                 roi=tuple(roi), area=tuple(roi), keyword='')
                    for name, roi in manifest['ocr'].items()}
        for rule in self.ocr.values():
            rule.score = .85
        self.last_state = None
        self.phase = 'idle'
        self.completed = 0
        self.outcome = 'NOT_COMPLETED'
        self._frame_image = None
        self._matches = {}
        self._state = None
        self._unknown_reported = False
        self._snapshots = 0
        self.unknown_since = None
        self._entry_frame_ready = False
        self.battle_stage = None
        self.battle_started_at = None
        self.score_status = 'NOT_DISPLAYED'
        self._next_score_at = 0
        self._score_hits, self._last_score = 0, None
        self._score_engine_errors = 0
        self._empty_score_scans = 0
        self._unreadable_log_at = -100.
        gb = getattr(task.conf, 'general_battle', None)
        self.early_exit = bool(getattr(gb, 'enable_boss_early_exit', False))
        self.score_limit = getattr(gb, 'boss_early_exit_score', 0)
        if self.early_exit and (not isinstance(self.score_limit, int) or self.score_limit <= 0):
            raise BossSafetyStop('Enabled early exit requires a positive configured score limit')

    def trace(self, event, **values):
        from module.logger import logger
        logger.info(f'[ShenmuBoss] {event} {values}')

    def capture(self):
        self.task.screenshot()
        # Clear on every acquisition, even if a backend reuses its ndarray.
        self._frame_image = self.device.image
        self._matches = {}
        self._state = None
        for rule in self.rules.values():
            rule.last_match_score = None

    def match(self, name):
        if self._frame_image is not self.device.image:
            self._frame_image, self._matches, self._state = self.device.image, {}, None
            for rule in self.rules.values():
                rule.last_match_score = None
        if name not in self._matches:
            self._matches[name] = self.rules[name].match(self.device.image)
        return self._matches[name]

    def candidates(self):
        return {'challenge': ('page_title', 'boss_name', 'fire'),
                'battle': ('score_label', 'exit', 'clock'),
                'battle_loading': ('exit', 'clock'),
                'confirm': ('confirm_title', 'confirm_button', 'confirm_score_label', 'exit', 'clock'),
                'daruma': ('daruma', 'continue', 'daruma_background'),
                'reward': ('reward_bag', 'reward_neck', 'settlement_background')}

    def state(self):
        image = self.device.image
        if image is None or image.shape[:2] != (720, 1280):
            raise BossSafetyStop('Expected normalized 1280x720 client screenshot')
        if self._frame_image is not image:
            self._frame_image, self._matches, self._state = image, {}, None
            for rule in self.rules.values():
                rule.last_match_score = None
        if self._state is not None:
            return self._state
        def has(*names):
            return all(self.match(name) for name in names)
        # A dark battle remains visible behind the exit dialog: dialog first.
        if has('confirm_title', 'confirm_button', 'confirm_score_label', 'exit', 'clock'):
            self._state = 'confirm'
        elif has('confirm_title') or has('confirm_button'):
            self._state = 'blocked'
        else:
            orders = {'idle': ('challenge', 'battle', 'daruma', 'reward'),
                      'entry': ('daruma', 'reward', 'battle', 'battle_loading', 'challenge'),
                      'battle': ('daruma', 'reward', 'battle', 'battle_loading', 'challenge'),
                      'exit': ('daruma', 'reward', 'battle', 'challenge'),
                      'settlement': ('reward', 'daruma', 'challenge', 'battle')}
            self._state = next((name for name in orders[self.phase]
                                if has(*self.candidates()[name])), 'unknown')
        return self._state

    def evidence(self, reason, elapsed=0):
        conditions = self.candidates()
        for names in conditions.values():
            for name in names:
                self.match(name)
        binding = vars(self.device)
        return dict(reason=reason, phase=self.phase, state=self.last_state,
                    pid=binding.get('root_handle'), hwnd=binding.get('root_handle_num'),
                    title=binding.get('root_handle_title'), screenshot_shape=list(self.device.image.shape),
                    unknown_seconds=round(elapsed, 3),
                    battle_stage=self.battle_stage, score_status=self.score_status,
                    empty_score_scans=self._empty_score_scans,
                    score_engine_errors=self._score_engine_errors,
                    candidates={state: {'failed': [n for n in names if not self.match(n)],
                                        'context_allowed': state != 'battle_loading' or self.phase in ('entry', 'battle')}
                                for state, names in conditions.items()},
                    templates={name: dict(score=rule.last_match_score, threshold=rule.threshold,
                                          roi=list(rule.roi_back), matched=self._matches.get(name),
                                          matched_roi=list(rule.roi_front) if self._matches.get(name) else None)
                               for name, rule in self.rules.items()},
                    outcome=self.outcome, completed=self.completed)

    def snapshot(self, reason, elapsed=0):
        """Only anomalous frames; never replace original desktop captures."""
        import cv2
        folder = Path('screenshots/diagnostics/shenmu_boss_runtime')
        try:
            if self._snapshots >= 2:
                return
            self._snapshots += 1
            folder.mkdir(parents=True, exist_ok=True)
            stem = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            image = self.device.image
            if image is not None:
                ok, data = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
                if ok:
                    with (folder/(stem+'.png')).open('xb') as output:
                        output.write(data.tobytes())
            with (folder/(stem+'.json')).open('x', encoding='utf-8') as output:
                json.dump(self.evidence(reason, elapsed), output, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.trace('snapshot_failed', error=type(exc).__name__)

    def click(self, expected, action):
        # Revalidate the current captured frame before every single input.
        if self.state() != expected:
            raise BossSafetyStop(f'State changed before {action}')
        rects = {'fire': (1148, 605, 48, 25), 'exit': (25, 23, 20, 18)}
        if action == 'settlement':
            if expected not in ('daruma', 'reward'):
                raise BossSafetyStop('Settlement input on wrong state')
            point = sample_settlement_point()
            if not settlement_point_safe(*point):
                raise BossSafetyStop('Settlement coordinate outside safe sides')
        else:
            allowed = {'fire': 'challenge', 'exit': 'battle', 'confirm': 'confirm'}
            if allowed.get(action) != expected:
                raise BossSafetyStop('Action/state mismatch')
            if action == 'confirm':
                # RuleImage records the peak location on this same captured frame.
                # Use the yellow button's interior, including its local animation offset.
                bx, by, _, _ = self.rules['confirm_button'].roi_front
                x, y, w, h = bx+30, by+9, 85, 22
            else:
                x, y, w, h = rects[action]
            point = random.randrange(x, x+w), random.randrange(y, y+h)
        self.trace('input_intent', state=expected, action=action, point=point)
        self.device.click(*point, control_name='SHENMU_'+action.upper(),
                          control_roi=(*point, 1, 1))
        self.trace('input_delivered', action=action, game_acceptance='unconfirmed')

    def limit_reached(self):
        conf = self.task.conf.general_climb
        duration = conf.limit_time_v
        return (self.task.current_count >= conf.boss_limit
                or (duration is not None and datetime.now()-self.task.start_time >= duration))

    def resources_enough(self):
        values = []
        for index in range(2):
            if index:
                pause(self.POLL)
                self.capture()
            if self.state() != 'challenge':
                raise BossSafetyStop('Challenge changed during resource preflight')
            try:
                values.append((parse_balance(self.ocr['tickets'].ocr_single_line(self.device.image)),
                               parse_cost(self.ocr['cost'].ocr_single_line(self.device.image))))
            except Exception as exc:
                raise BossSafetyStop(f'Resource OCR error: {type(exc).__name__}', 'OCR_FAILED') from exc
        if any(None in row for row in values) or values[0] != values[1]:
            raise BossSafetyStop('Resource OCR invalid or unstable', 'OCR_FAILED')
        balance, cost = values[-1]
        self.trace('resources', balance_lower_bound=balance, cost=cost)
        return balance >= cost

    def score_digits_visible(self):
        """Cheap readiness candidate, not an OCR value or proof of a score.

        The orange digit strokes must form a tall connected component within
        the existing number-only ROI. Background specks alone never authorize
        exit; the original strict OCR and three fresh-frame checks still apply.
        """
        import cv2
        x, y, w, h = self.ocr['score'].roi
        crop = self.device.image[y:y+h, x:x+w]
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_RGB2HSV), (0, 100, 120), (40, 255, 255))
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        return any(height >= 18 and area >= 40 for _, _, _, height, area in stats[1:])

    def score_unavailable(self, now, status):
        self.score_status = status
        self._score_hits, self._last_score = 0, None
        if now-self._unreadable_log_at >= self.SCORE_DIAGNOSTIC_INTERVAL:
            self._unreadable_log_at = now
            self.trace('score_wait', battle_stage=self.battle_stage, status=status,
                       battle_elapsed=round(now-self.battle_started_at, 2),
                       empty_scans=self._empty_score_scans)

    def monitor_score(self, now, *, title_ready=True):
        if not title_ready:
            self.score_unavailable(now, 'NOT_DISPLAYED')
            return False
        if now < self._next_score_at:
            return False
        interval = (self.LOADING_SCORE_INTERVAL if self.battle_stage == 'BATTLE_LOADING'
                    else self.SCORE_INTERVAL)
        self._next_score_at = now+interval
        if not self.score_digits_visible():
            self.score_unavailable(now, 'NOT_DISPLAYED')
            return False
        try:
            score = parse_number(self.ocr['score'].ocr_single_line(self.device.image))
            self._score_engine_errors = 0
        except Exception as exc:
            self._score_engine_errors += 1
            self.score_unavailable(now, 'OCR_ENGINE_ERROR')
            self.trace('score_ocr_error', error=type(exc).__name__, consecutive=self._score_engine_errors)
            if self._score_engine_errors >= 3:
                raise BossSafetyStop('Score OCR engine raised three consecutive exceptions', 'OCR_ENGINE_ERROR') from exc
            return False
        if score is None:
            self._empty_score_scans += 1
            self.score_unavailable(now, 'NOT_READABLE' if self.battle_stage == 'BATTLE_LOADING'
                                   else 'TEMPORARILY_UNREADABLE')
            return False  # Empty/low confidence text is not an OCR engine exception.
        if self.battle_stage == 'BATTLE_LOADING':
            self.battle_stage = 'BATTLE_MONITORING'
            self.trace('score_ready', battle_stage=self.battle_stage, value=score,
                       battle_elapsed=round(now-self.battle_started_at, 2))
        self.score_status = 'READABLE'
        self._score_hits = (self._score_hits+1 if score > self.score_limit
                            and (self._last_score is None or score >= self._last_score) else 0)
        self._last_score = score if self._score_hits else None
        self.trace('score', value=score, threshold=self.score_limit, comparison='>', consecutive=self._score_hits)
        if self._score_hits:
            self._next_score_at = now+self.POLL  # Only confirming a high score uses the faster cadence.
        return self._score_hits >= 3

    def prepare_entry(self):
        """Only a positively identified courtyard may use the legacy navigation."""
        from tasks.GameUi.page import page_main
        from tasks.ActivityShikigami.page import page_climb_act
        self.capture()
        self._entry_frame_ready = True
        if self.state() != 'unknown':
            return
        if not self.task.ui_page_appear(page_main):
            return  # Unknown never authorizes navigation or generic confirms.
        self.trace('entry_courtyard_confirmed')
        self.task.ui_get_current_page()
        self.task.switch_soul()
        if not self.task.ui_goto(page_climb_act):
            raise BossSafetyStop('Legacy courtyard activity navigation failed', 'PAGE_UNKNOWN')
        # Same existing BOSS entry assets; add the previously missing bound.
        if not self.task.ui_click(self.task.I_TO_BATTLE_BOSS,
                                  stop=self.task.I_CHECK_BATTLE_BOSS, interval=1, timeout=25):
            raise BossSafetyStop('Legacy BOSS page navigation timed out', 'PAGE_UNKNOWN')
        self.capture()  # The loop must confirm the current BOSS, not trust a navigation return.

    def loop(self):
        phase = 'idle'
        phase_since = monotonic()
        previous, stable, state_since = None, 0, monotonic()
        unknown_since = None
        exit_clicks = confirm_clicks = 0
        quit_requested = False
        saw_reward = False
        last_click = -100.
        settle_clicks = {'daruma': 0, 'reward': 0}
        entry_observed = False
        self.device.click_record_clear()
        while True:
            self.phase = phase
            if self._entry_frame_ready:
                self._entry_frame_ready = False
            else:
                self.capture()
            now = monotonic()
            state = self.state()
            self.last_state = state
            if not entry_observed and state in ('challenge', 'battle'):
                entry_observed = True
                self.trace('entry_confirmed', state=state, source='visual')
            stable = stable+1 if state == previous else 1
            if state != previous:
                state_since = now
                self.trace('state', state=state, phase=phase)
                previous = state
            if state == 'unknown':
                self._score_hits, self._last_score = 0, None
                unknown_since = now if unknown_since is None else unknown_since
                # After our single challenge input, entry animation owns its
                # existing 25s deadline; a missing score cannot shorten it.
                if phase != 'entry' and now-unknown_since > self.UNKNOWN_TIMEOUT:
                    raise BossSafetyStop('Unknown screen timeout; no blind continuation', 'PAGE_UNKNOWN')
                if not self._unknown_reported:
                    self._unknown_reported = True
                    self.trace('unknown_evidence', **self.evidence('first_unknown', now-unknown_since))
                    self.snapshot('first_unknown', now-unknown_since)
            else:
                unknown_since = None
            self.unknown_since = unknown_since
            if state == 'blocked':
                # No input during this bounded grace. Only our own score exit,
                # its specific title and all battle HUD anchors qualify for waiting.
                if (phase == 'exit' and quit_requested
                        and now-last_click < self.EXIT_DIALOG_GRACE
                        and all(self.match(name) for name in
                                ('confirm_title', 'confirm_score_label', 'exit', 'clock'))):
                    pause(self.POLL)
                    continue
                raise BossSafetyStop('Unrecognized dialog; no generic confirmation')

            timeout = {'entry': self.ENTRY_TIMEOUT, 'battle': self.BATTLE_TIMEOUT,
                       'exit': self.EXIT_TIMEOUT, 'settlement': self.SETTLEMENT_TIMEOUT}.get(phase)
            if phase == 'entry' and state in ('battle', 'battle_loading'):
                timeout = None  # Positive arrival evidence wins over an expired entry wait.
            if phase == 'battle' and now-phase_since > self.BATTLE_TIMEOUT:
                # One final fresh observation can reveal natural settlement.
                # No timeout path sends exit or confirmation input.
                self.capture()
                state = self.state()
                self.last_state = state
                now = monotonic()
                if state not in ('daruma', 'reward'):
                    raise BossSafetyStop('battle timeout; safety limit reached, current score/end unconfirmed',
                                         'SCORE_NOT_READY' if self.early_exit and self.battle_stage == 'BATTLE_LOADING'
                                         else 'BATTLE_TIMEOUT')
                stable, state_since = 1, now
                timeout = None
            if timeout is not None and now-phase_since > timeout:
                raise BossSafetyStop(f'{phase} timeout; no automatic challenge retry',
                                     'SETTLEMENT_UNCONFIRMED' if phase == 'settlement' else 'SAFE_STOP')

            if state == 'challenge':
                self._score_hits, self._last_score = 0, None
                if phase in ('battle', 'exit'):
                    raise BossSafetyStop('Returned to challenge without observed settlement',
                                         'SETTLEMENT_UNCONFIRMED')
                if phase == 'settlement':
                    if not saw_reward:
                        raise BossSafetyStop('Returned without reward page', 'SETTLEMENT_UNCONFIRMED')
                    if stable >= 2:
                        self.completed += 1
                        self.trace('round_complete', count=self.task.current_count, completed=self.completed,
                                   reward_retention='PENDING_LIVE_VALIDATION')
                        phase, phase_since = 'idle', now
                        saw_reward = False
                        settle_clicks = {'daruma': 0, 'reward': 0}
                        exit_clicks = confirm_clicks = 0
                        quit_requested = False
                # Entry pending: never click a second challenge, even if visible.
                if phase == 'idle' and stable >= 3 and now-state_since >= .8:
                    if self.limit_reached() or not self.resources_enough():
                        self.outcome = 'COMPLETED' if self.limit_reached() else 'RESOURCE_EXHAUSTED'
                        return
                    if self.limit_reached():
                        self.outcome = 'COMPLETED'
                        return
                    self.click('challenge', 'fire')
                    phase, phase_since = 'entry', monotonic()
            elif state in ('battle', 'battle_loading'):
                if phase in ('idle', 'entry'):
                    # Direct resume is supported, without challenge/preparation inputs.
                    self.task.current_count += 1
                    self.task.count_map['boss'] = self.task.current_count
                    phase, phase_since = 'battle', now
                    self._next_score_at = now
                    self._score_hits, self._last_score = 0, None
                    self._score_engine_errors = self._empty_score_scans = 0
                    self._unreadable_log_at = -100.
                    self.battle_stage, self.battle_started_at = 'BATTLE_LOADING', now
                    self.device.stuck_record_add('BATTLE_STATUS_S')
                    self.trace('battle_started', count=self.task.current_count, battle_stage=self.battle_stage)
                if phase not in ('battle', 'exit'):
                    raise BossSafetyStop('Unexpected battle after settlement')
                if phase == 'exit':
                    if state == 'battle' and confirm_clicks == 0 and exit_clicks < 2 and now-last_click >= 3:
                        self.click('battle', 'exit')
                        exit_clicks += 1
                        last_click = monotonic()
                elif self.early_exit:
                    if self.monitor_score(now, title_ready=state == 'battle'):
                        self.click('battle', 'exit')
                        self.battle_stage = 'BATTLE_EARLY_EXIT'
                        quit_requested = True
                        exit_clicks = 1
                        last_click = monotonic()
                        phase, phase_since = 'exit', last_click
            elif state == 'confirm':
                self._score_hits, self._last_score = 0, None
                if phase != 'exit' or not quit_requested:
                    raise BossSafetyStop('Exit dialog without script score decision; not confirmed')
                if stable >= 2 and now-last_click >= 1:
                    if confirm_clicks >= 3:
                        raise BossSafetyStop('Exit dialog unchanged after three confirms')
                    self.click('confirm', 'confirm')
                    confirm_clicks += 1
                    last_click = monotonic()
            elif state in ('daruma', 'reward'):
                self._score_hits, self._last_score = 0, None
                if phase not in ('battle', 'exit', 'settlement'):
                    raise BossSafetyStop('Settlement without observed battle')
                if phase != 'settlement':
                    if not quit_requested:
                        self.battle_stage = 'BATTLE_NATURAL_END'
                        self.trace('battle_natural_end', state=state)
                    phase, phase_since = 'settlement', now
                if state == 'daruma':
                    if saw_reward:
                        raise BossSafetyStop('Unexpected daruma after reward')
                else:
                    saw_reward = True
                if stable >= 2 and now-last_click >= 2:
                    if settle_clicks[state] >= 3:
                        raise BossSafetyStop('Settlement unchanged after three single clicks')
                    self.click(state, 'settlement')
                    settle_clicks[state] += 1
                    last_click = monotonic()
            pause(.5 if state in ('unknown', 'battle', 'battle_loading') and not self._score_hits
                  else self.POLL)


def run_shenmu_boss(task):
    """Use legacy navigation only from a confirmed courtyard; keep BOSS inputs local."""
    from module.exception import TaskEnd, RequestHumanTakeover
    from module.logger import logger
    runner = None
    success = False
    stop_reason = None
    try:
        runner = ShenmuBoss(task)
        runner.trace('start', score_limit=runner.score_limit, early_exit=runner.early_exit, comparison='>',
                     reward_forbidden_y=REWARD_Y_BAND, entry='current_page_requested',
                     entry_confirmed=False)
        runner.prepare_entry()
        runner.loop()
        success = True
    except Exception as exc:
        stop_reason = f'{type(exc).__name__}: {exc}'
        logger.error(f'[ShenmuBoss] stopped: {type(exc).__name__}: {exc}')
        if runner is not None:
            runner.outcome = getattr(exc, 'outcome', 'SAFE_STOP')
            elapsed = monotonic()-runner.unknown_since if runner.unknown_since is not None else 0
            runner.snapshot(f'{type(exc).__name__}: {exc}', elapsed)
        if not isinstance(exc, (BossSafetyStop, ValueError, RuntimeError)):
            raise
    finally:
        if runner is not None:
            runner.trace('result', outcome=runner.outcome, phase=runner.phase, battle_stage=runner.battle_stage,
                         score_status=runner.score_status, completed=runner.completed,
                         success=success, reward_retention='PENDING_LIVE_VALIDATION')
        task.set_next_run(task='ActivityShikigami', success=success)
    if not success:
        # Existing Script._run_task stops this instance on human takeover.
        # Returning TaskEnd here would allow idle GotoMain/other scheduled input
        # while this BOSS battle or settlement is still unresolved.
        raise RequestHumanTakeover(f'[ShenmuBoss] automatic input stopped; preserve current screen: {stop_reason}')
    raise TaskEnd('ActivityShikigami')
