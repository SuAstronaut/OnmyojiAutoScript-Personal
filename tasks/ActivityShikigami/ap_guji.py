"""1280x720 古迹演武 AP adapter; no device access at import time.

Only this activity uses these rules. Existing pass/boss and public battle rules
remain unchanged. Screenshot provenance and offline results: GUJI_AP.md.
"""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from enum import Enum
from pathlib import Path
import random
import re
from time import monotonic, sleep


class ApSafetyStop(RuntimeError):
    """Unknown evidence: save the current frame, do not confirm or buy."""


class ApState(str, Enum):
    COURTYARD = 'courtyard'
    HOME = 'home'
    CHALLENGE = 'challenge'
    PREPARE = 'prepare'
    BATTLE = 'battle'
    VICTORY = 'victory'
    REWARD = 'reward'
    DIALOG = 'dialog'
    FAILED = 'failed'
    UNKNOWN = 'unknown'


class X5State(str, Enum):
    ON = 'ON'
    OFF = 'OFF'
    UNKNOWN = 'UNKNOWN'


def parse_quantity(text, *, abbreviated=False):
    """Strict raw text; abbreviated AP is a conservative lower bound.

    Subtract one display quantum: 3.4万 -> 33000. Never interpret a counter,
    an OCR error or a missing unit as a positive balance.
    """
    if not isinstance(text, str):
        raise ApSafetyStop('Resource OCR is not text')
    text = text.strip()
    if re.fullmatch(r'[0-9]{1,9}', text):
        return int(text)
    if abbreviated and re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,2})?万', text):
        number = text[:-1]
        places = len(number.split('.')[1]) if '.' in number else 0
        quantum = Decimal(10000) / (10 ** places)
        return max(0, int(Decimal(number) * 10000 - quantum))
    raise ApSafetyStop(f'Unsupported resource format: {text!r}')


def parse_cost(text):
    if not isinstance(text, str) or not re.fullmatch(r'[×xX]\s*[1-9][0-9]?', text.strip()):
        raise ApSafetyStop(f'Unknown consumption: {text!r}')
    return int(text.strip()[1:].strip())


@dataclass(frozen=True)
class ApResources:
    ap: int
    activity: int
    x5: int
    ap_cost: int
    activity_cost: int

    @property
    def enough(self):
        return self.ap >= self.ap_cost and self.activity >= self.activity_cost


def resources_from_text(ap, activity, x5, ap_cost, activity_cost):
    result = ApResources(parse_quantity(ap, abbreviated=True),
                         parse_quantity(activity), parse_quantity(x5),
                         parse_cost(ap_cost), parse_cost(activity_cost))
    # Current single-consumption rule verified by the original screenshot and
    # user confirmation (2026-10-10): 6 AP AND 1 mandatory activity resource.
    if (result.ap_cost, result.activity_cost) != (6, 1):
        raise ApSafetyStop('Consumption differs from the verified 6 AP + 1 rule')
    return result


def x5_from_matches(on, off):
    if on == off:
        return X5State.UNKNOWN
    return X5State.ON if on else X5State.OFF


def safe_settlement_point(x, y):
    return (20 <= x <= 1260 and 20 <= y <= 700
            and not (240 <= x <= 1080 and 150 <= y <= 440)
            and not (820 <= x <= 1280 and 0 <= y <= 65))


def sample_settlement_point(rng=random):
    # Uniform integer rejection sampling over the entire allowed screen area.
    while True:
        point = rng.randint(20, 1260), rng.randint(20, 700)
        if safe_settlement_point(*point):
            return point


def ap_limit_reached(count, limit, elapsed, duration):
    return count >= limit or (duration is not None and elapsed >= duration)


class ApGujiMixin:
    AP_NAV_TIMEOUT = 35
    AP_ENTRY_TIMEOUT = 25
    AP_BATTLE_TIMEOUT = 240
    AP_SETTLE_TIMEOUT = 30
    AP_UNKNOWN_TIMEOUT = 12
    AP_ENTRY_POLL = .10
    AP_SETTLE_POLL = .15
    AP_STAGE_CLICK_GAP = .8
    AP_RETRY_CLICK_GAP = 2
    AP_CHALLENGE_STABLE = .9
    AP_CHALLENGE_READY_TIMEOUT = 8

    def ap_trace(self, event, **values):
        from module.logger import logger
        fields = ' '.join(f'{key}={value}' for key, value in values.items())
        logger.info(f'AP_GUJI_TRACE {event} t={monotonic():.3f} count={self.current_count} {fields}')

    def ap_diagnostic_trace(self, event, **values):
        # New diagnostics must never turn an input/classification into a failure.
        try:
            self.ap_trace(event, **values)
        except Exception:
            pass

    def ap_fire_input_trace(self, phase, x, y, requested, returned=None, error=None):
        values = dict(phase=phase, request_timestamp=round(requested, 6),
                      backend_return_timestamp=round(returned, 6) if returned is not None else 'pending',
                      duration=round(returned-requested, 6) if returned is not None else 'pending',
                      duration_scope='request_to_backend_return', x=x, y=y, raw_x=x, raw_y=y,
                      coordinate_space='1280x720', converted_x='not_exposed', converted_y='not_exposed',
                      conversion='not_exposed', press_duration='not_exposed',
                      input_sent='not_exposed', game_acceptance='unconfirmed', method='not_exposed',
                      backend_status='raised' if error is not None else ('returned' if returned is not None else 'pending'))
        try:
            values['method'] = self.device.config.script.device.control_method
            if returned is not None and values['method'] == 'window_message':
                if getattr(self.device, 'is_desktop_window', False):
                    mx, my = self.device.desktop_message_coord(x, y)
                    values.update(converted_x=mx, converted_y=my, conversion='post_return_projection')
                elif hasattr(self.device, 'window_scale_rate'):
                    scale = self.device.window_scale_rate
                    values.update(converted_x=int(x/scale), converted_y=int(y/scale),
                                  conversion='post_return_projection')
        except Exception as exc:
            values['metadata_error'] = type(exc).__name__
        if error is not None:
            values['backend_error'] = type(error).__name__
        self.ap_diagnostic_trace('fire_input', **values)

    def ap_session_miss_trace(self, reason, rule=None, **values):
        # Inspect only the failed rule on the SAME existing frame. Do not call
        # rule.match again (it moves roi_front), OCR, or any later session check.
        if rule is not None:
            try:
                import cv2
                import numpy as np
                x, y, w, h = rule.roi_back
                template = rule.image
                scores = cv2.matchTemplate(self.device.image[y:y+h, x:x+w], template,
                                           cv2.TM_CCOEFF_NORMED)
                _, ncc, _, loc = cv2.minMaxLoc(scores)
                px, py = x+loc[0], y+loc[1]
                patch = self.device.image[py:py+template.shape[0], px:px+template.shape[1]]
                values.update(rule=rule.name, roi=(px, py, template.shape[1], template.shape[0]),
                              ncc=round(float(ncc), 6), ncc_threshold=rule.threshold,
                              mae=round(float(np.abs(patch.astype(float)-template.astype(float)).mean()), 6),
                              mae_threshold=28, metric_source='same_frame_readonly_recompute')
            except Exception as exc:
                values['metric_error'] = type(exc).__name__
        self.ap_diagnostic_trace('session_miss', reason=reason, first_failed_reason=reason,
                                 later_checks='not_evaluated', **values)

    def ap_invalidate_session(self):
        self._ap_session_key = None
        self._ap_cost_frames = None

    def ap_observe_phase(self, state):
        times = getattr(self, '_ap_phase_seen', None)
        if times is None:
            times = self._ap_phase_seen = {}
        if state in (ApState.VICTORY, ApState.REWARD, ApState.CHALLENGE) and state not in times:
            times[state] = monotonic()
            self.ap_trace('first_seen', state=state.value)
        return times

    def ap_session_key(self):
        return (self.conf.general_climb.five_times_enable,
                self.conf.general_battle.enable_ap_preset)

    def ap_cost_frames(self):
        # Only validated static consumption text; never cache changing balances.
        frames = []
        for rule in (self.O_AP_GUJI_AP_COST, self.O_AP_GUJI_ACTIVITY_COST):
            x, y, w, h = rule.roi
            frames.append(self.device.image[y:y+h, x:x+w].copy())
        return frames

    def ap_session_matches(self):
        import cv2
        import numpy as np
        cached_key = getattr(self, '_ap_session_key', None)
        current_key = self.ap_session_key()
        if cached_key != current_key:
            self.ap_session_miss_trace('cache_missing' if cached_key is None else 'session_key_changed',
                                      check='session_key', actual=cached_key, expected=current_key)
            return False
        if getattr(self, '_ap_cost_frames', None) is None:
            self.ap_session_miss_trace('cache_missing', check='cost_frames', actual='None', expected='present')
            return False
        x5 = self.ap_x5_state()
        if x5 != X5State.OFF:
            self.ap_session_miss_trace('five_times_failed', actual=x5.value, expected=X5State.OFF.value)
            return False
        target = self.I_AP_GUJI_UNLOCK if self.conf.general_battle.enable_ap_preset else self.I_AP_GUJI_LOCK
        opposite = self.I_AP_GUJI_LOCK if self.conf.general_battle.enable_ap_preset else self.I_AP_GUJI_UNLOCK
        lock_reason = 'unlock_failed' if self.conf.general_battle.enable_ap_preset else 'lock_failed'
        if not self.ap_match(target):
            self.ap_session_miss_trace(lock_reason, target, check='target_present', actual=False, expected=True)
            return False
        if self.ap_match(opposite):
            self.ap_session_miss_trace(lock_reason, opposite, check='opposite_absent', actual=True, expected=False)
            return False
        for rule, reason in ((self.I_AP_GUJI_AP_COST, 'ap_icon_failed'),
                             (self.I_AP_GUJI_ACTIVITY_COST, 'activity_icon_failed')):
            if not self.ap_match(rule):
                self.ap_session_miss_trace(reason, rule, actual=False, expected=True)
                return False
        for index, (old, new) in enumerate(zip(self._ap_cost_frames, self.ap_cost_frames())):
            text = 'ap' if index == 0 else 'activity'
            if old.shape != new.shape:
                self.ap_session_miss_trace(f'{text}_text_shape_failed', actual=new.shape, expected=old.shape)
                return False
            mae = np.abs(old.astype(float)-new.astype(float)).mean()
            if mae > 12:
                self.ap_session_miss_trace(f'{text}_text_mae_failed', actual=round(float(mae), 6),
                                          mae=round(float(mae), 6), threshold=12)
                return False
            similarity = cv2.matchTemplate(new, old, cv2.TM_CCOEFF_NORMED)[0, 0]
            if not np.isfinite(similarity) or similarity < .97:
                self.ap_session_miss_trace(f'{text}_text_ncc_failed', actual=float(similarity),
                                          ncc=float(similarity), threshold=.97,
                                          finite=bool(np.isfinite(similarity)))
                return False
        return True

    def ap_insufficient(self):
        # Cheap template on every entry frame; exact text confirmation only on
        # a candidate, using that SAME frame so a transient toast cannot vanish.
        if not self.ap_match(self.I_AP_GUJI_INSUFFICIENT):
            return False
        x, y, w, h = self.I_AP_GUJI_INSUFFICIENT.roi_front
        rule = self.O_AP_GUJI_INSUFFICIENT
        try:
            # Follow the matched toast if its position moved within search ROI.
            from copy import copy
            rule = copy(rule)
            rule.roi = [x, y, w, h]
            text = rule.ocr(self.device.image)
        except Exception as exc:
            raise ApSafetyStop(f'Insufficient-resource candidate OCR failed: {type(exc).__name__}') from exc
        if not isinstance(text, str) or text.strip() != '琥珀卷轴不足':
            raise ApSafetyStop('Unconfirmed resource warning; no further challenge clicks')
        self._ap_resource_exhausted = True
        self.ap_invalidate_session()
        self.ap_trace('resource_exhausted', reason='琥珀卷轴不足', normal_stop=True)
        return True

    def ap_match(self, rule):
        import numpy as np
        if not rule.match(self.device.image):
            return False
        # NCC alone also matches dimmed background under purchase dialogs.
        # Require original brightness/color; do not click through an overlay.
        x, y, w, h = rule.roi_front
        patch = self.device.image[y:y+h, x:x+w]
        return (patch.shape == rule.image.shape
                and np.abs(patch.astype(float) - rule.image.astype(float)).mean() <= 28)

    def ap_state(self):
        if self.device.image.shape[:2] != (720, 1280):
            raise ApSafetyStop('AP adapter requires a full 1280x720 frame')
        if (self.appear(self.I_UI_CONFIRM) or self.appear(self.I_UI_CONFIRM_SAMLL)
                or self.appear(self.I_EXIT_ENSURE)):
            return ApState.DIALOG
        if self.appear(self.I_FALSE):
            return ApState.FAILED
        cont = (self.ap_match(self.I_AP_GUJI_CONTINUE)
                or self.ap_match(self.I_AP_GUJI_CONTINUE_REWARD))
        win = cont and self.ap_match(self.I_AP_GUJI_VICTORY)
        reward = cont and self.ap_match(self.I_AP_GUJI_REWARD)
        if win and reward:
            return ApState.UNKNOWN
        if win:
            return ApState.VICTORY
        if reward:
            return ApState.REWARD
        if (self.ap_match(self.I_AP_GUJI_CHALLENGE)
                and self.ap_match(self.I_AP_GUJI_FIRE)
                and self.ap_match(self.I_AP_GUJI_AP_TOP)
                and self.ap_match(self.I_AP_GUJI_ACTIVITY_TOP)):
            return ApState.CHALLENGE
        if self.ap_match(self.I_AP_GUJI_HOME) and self.ap_match(self.I_AP_GUJI_ROUTE):
            return ApState.HOME
        from tasks.GameUi.page import page_main
        if self.ui_page_appear(page_main):
            return ApState.COURTYARD
        if self.is_in_real_battle(False):
            return ApState.BATTLE
        if self.is_in_prepare(False):
            return ApState.PREPARE
        return ApState.UNKNOWN

    def screenshot(self):
        # Bound the existing preparation / preset / soul-switch helpers without
        # changing public modules. Their loops all take screenshots.
        deadline = getattr(self, '_ap_guard_deadline', None)
        if deadline is not None and monotonic() >= deadline:
            raise ApSafetyStop('AP preparation/navigation helper timed out')
        if getattr(self, '_ap_guji_active', False):
            # AP snapshots must be input-free: BaseTask._burst can accept an
            # invitation while taking a frame. Preserve HOT/device guards but
            # leave unknown overlays to this adapter's fail-closed classifier.
            self.config.refresh_hot_at_checkpoint(self)
            self.device.screenshot()
            self._reward_safe_rules = None
            result = self.device.image
        else:
            result = super().screenshot()
        if getattr(self, '_ap_guji_active', False) and deadline is not None:
            if (self.appear(self.I_UI_CONFIRM) or self.appear(self.I_UI_CONFIRM_SAMLL)
                    or self.appear(self.I_EXIT_ENSURE)):
                raise ApSafetyStop('Unapproved confirmation dialog; no confirmation clicked')
        return result

    def ap_snapshot(self, reason, image=None):
        import cv2
        from module.logger import logger
        try:
            frame = image if image is not None else getattr(self.device, 'image', None)
            if frame is None:
                return
            folder = Path('screenshots/ActivityShikigami_Screenshots/ap_guji')
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            path = folder / f'{stamp}_{re.sub(r"[^a-zA-Z0-9_-]", "_", reason)[:60]}.png'
            cv2.imencode('.png', cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))[1].tofile(path)
            logger.warning(f'AP stopped; current frame saved to {path}')
        except Exception as exc:
            logger.warning(f'AP snapshot unavailable: {exc}')

    def ap_click_rect(self, rect, name):
        x, y, w, h = rect
        px, py = random.randint(x, x+w-1), random.randint(y, y+h-1)
        fire = name == 'AP_GUJI_FIRE'
        if fire:
            requested = monotonic()
            self.ap_fire_input_trace('request', px, py, requested)
        # One device click: no Rule coord humanizer resampling, no multi-click.
        try:
            self.device.click(px, py, control_name=name, control_roi=rect)
        except Exception as exc:
            if fire:
                self.ap_fire_input_trace('backend_return', px, py, requested, monotonic(), error=exc)
            raise
        if fire:
            self.ap_fire_input_trace('backend_return', px, py, requested, monotonic())

    def ap_click_match(self, rule, inset=2):
        if not self.ap_match(rule):
            raise ApSafetyStop(f'Click target lost: {rule.name}')
        x, y, w, h = rule.roi_front
        self.ap_click_rect((x+inset, y+inset, w-2*inset, h-2*inset), rule.name)

    def ap_wait_for(self, target, timeout):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            self.screenshot()
            state = self.ap_state()
            if state in (ApState.DIALOG, ApState.FAILED):
                raise ApSafetyStop(f'Unexpected {state.value}; no confirmation clicked')
            if state == target:
                return
            sleep(.5)
        raise ApSafetyStop(f'Timeout waiting for {target.value}')

    def ap_navigate(self):
        self.ap_invalidate_session()
        self._ap_resource_exhausted = False
        self._ap_phase_seen = {}
        self._ap_last_reward_click = None
        deadline, refreshes, entries = monotonic()+self.AP_NAV_TIMEOUT, 0, 0
        last_click = -100.0
        while monotonic() < deadline:
            self.screenshot()
            state = self.ap_state()
            if state in (ApState.CHALLENGE, ApState.BATTLE, ApState.PREPARE,
                         ApState.VICTORY, ApState.REWARD):
                return state
            if state in (ApState.DIALOG, ApState.FAILED):
                raise ApSafetyStop(f'Navigation blocked by {state.value}')
            if monotonic()-last_click >= 2:
                if state == ApState.COURTYARD:
                    if self.ap_match(self.I_AP_GUJI_ENTRY):
                        if entries >= 3:
                            raise ApSafetyStop('Activity entry did not open')
                        self.ap_click_match(self.I_AP_GUJI_ENTRY, 5)
                        entries += 1
                    elif refreshes < 8 and self.ap_match(self.I_AP_GUJI_REFRESH):
                        self.ap_click_match(self.I_AP_GUJI_REFRESH)
                        refreshes += 1
                    else:
                        raise ApSafetyStop('Activity icon/refresh button unavailable')
                    last_click = monotonic()
                elif state == ApState.HOME:
                    if getattr(self, '_ap_route_attempts', 0) >= 3:
                        raise ApSafetyStop('Guji entry did not open')
                    self.ap_click_match(self.I_AP_GUJI_ROUTE, 0)
                    last_click = monotonic()
                    # No legacy intermediate page. At most three confirmed
                    # HOME clicks, each followed by a fresh frame.
                    self._ap_route_attempts = getattr(self, '_ap_route_attempts', 0)+1
            sleep(.5)
        raise ApSafetyStop('Activity navigation timed out')

    def ap_return_home(self):
        for expected, target in ((ApState.CHALLENGE, ApState.HOME),
                                 (ApState.HOME, ApState.COURTYARD)):
            self.screenshot()
            state = self.ap_state()
            if state == ApState.COURTYARD:
                return
            if state != expected:
                if expected == ApState.CHALLENGE and state == ApState.HOME:
                    continue
                raise ApSafetyStop('Cannot return from an unrecognized page')
            self.ap_click_match(self.I_AP_GUJI_BACK)
            self.ap_wait_for(target, 12)

    def ap_x5_state(self):
        return x5_from_matches(self.ap_match(self.I_AP_GUJI_X5_ON),
                               self.ap_match(self.I_AP_GUJI_X5_OFF))

    def ap_ensure_single(self):
        if self.conf.general_climb.five_times_enable:
            raise ApSafetyStop('This AP adapter requires five_times_enable=False')
        self.screenshot()
        if self.ap_state() != ApState.CHALLENGE:
            raise ApSafetyStop('Challenge page lost before x5 check')
        state = self.ap_x5_state()
        if state == X5State.UNKNOWN:
            raise ApSafetyStop('Five-times switch UNKNOWN')
        if state == X5State.ON:
            self.ap_click_match(self.I_AP_GUJI_X5_ON)
            deadline = monotonic()+8
            while monotonic() < deadline:
                sleep(.5)
                self.screenshot()
                if self.ap_state() != ApState.CHALLENGE:
                    raise ApSafetyStop('Page changed while disabling x5')
                if self.ap_x5_state() == X5State.OFF:
                    break
            else:
                raise ApSafetyStop('Five-times switch did not turn OFF')
        self._x5_active = False

    def ap_ensure_lock(self):
        wanted = not self.conf.general_battle.enable_ap_preset
        target = self.I_AP_GUJI_LOCK if wanted else self.I_AP_GUJI_UNLOCK
        opposite = self.I_AP_GUJI_UNLOCK if wanted else self.I_AP_GUJI_LOCK
        for attempt in range(4):
            self.screenshot()
            if self.ap_state() != ApState.CHALLENGE:
                raise ApSafetyStop('Challenge page lost before lineup check')
            found, other = self.ap_match(target), self.ap_match(opposite)
            if found and not other:
                return
            if found == other or attempt == 3:
                raise ApSafetyStop('Lineup lock state unknown or did not change')
            self.ap_click_match(opposite)
            sleep(1.5)

    def ap_read_resources(self):
        if self.ap_state() != ApState.CHALLENGE:
            raise ApSafetyStop('Cannot read resources outside Guji challenge')
        for rule in (self.I_AP_GUJI_AP_COST, self.I_AP_GUJI_ACTIVITY_COST):
            if not self.ap_match(rule):
                raise ApSafetyStop('AP/activity consumption icon identity missing')
        if self.ap_x5_state() != X5State.OFF:
            raise ApSafetyStop('Five-times OFF not positively confirmed')
        try:
            result = resources_from_text(*(rule.ocr(self.device.image) for rule in (
                self.O_AP_GUJI_AP, self.O_AP_GUJI_ACTIVITY, self.O_AP_GUJI_X5,
                self.O_AP_GUJI_AP_COST, self.O_AP_GUJI_ACTIVITY_COST)))
        except ApSafetyStop:
            raise
        except Exception as exc:
            raise ApSafetyStop(f'Resource OCR failed: {type(exc).__name__}') from exc
        from module.logger import logger
        logger.info(f'Guji AP resources (AP is a lower bound): {result}')
        return result

    def ap_check_resources(self):
        if self.ap_state() != ApState.CHALLENGE:
            self.ap_invalidate_session()
            raise ApSafetyStop('Resource preflight requires a confirmed challenge page')
        if self.ap_session_matches():
            # Permission to ATTEMPT entry, not proof of the current balance.
            # Only a positively identified battle advances the battle count.
            return True
        self.ap_invalidate_session()
        self.ap_ensure_single()
        self.ap_ensure_lock()
        first = self.ap_read_resources()
        sleep(.5)
        self.screenshot()
        second = self.ap_read_resources()
        if first != second:
            raise ApSafetyStop('Resource OCR not stable across two frames')
        self._ap_resources = second
        self._ap_session_key = self.ap_session_key()
        self._ap_cost_frames = self.ap_cost_frames()
        self.ap_trace('session_validated', resources=second)
        if not second.enough:
            self._ap_resource_exhausted = True
            self.ap_trace('resource_exhausted', reason='initial_resource_OCR', normal_stop=True)
        return second.enough

    def ap_should_stop(self):
        duration = self.conf.general_climb.limit_time_v
        return ap_limit_reached(self.current_count, self.conf.general_climb.ap_limit,
                                datetime.now()-self.start_time, duration)

    def ap_wait_challenge_ready(self):
        """Visual readiness window, not a claim that Windows/game accepted input.

        Never infer readiness from a lone transition frame. Reset the window
        on missing controls, button movement, brightness change or config change.
        No resource OCR here; the existing session preflight remains responsible
        for consumption validation.
        """
        import cv2
        import numpy as np
        deadline = monotonic()+self.AP_CHALLENGE_READY_TIMEOUT
        since, frames, anchor_roi, anchor_patch, anchor_config = None, 0, None, None, None
        while monotonic() < deadline:
            self.screenshot()
            if self.ap_insufficient():
                return None
            state = self.ap_state()
            if state in (ApState.DIALOG, ApState.FAILED):
                raise ApSafetyStop(f'Challenge readiness blocked by {state.value}; never confirm')
            if state in (ApState.BATTLE, ApState.PREPARE, ApState.VICTORY, ApState.REWARD):
                return state  # Take over an already started battle, never click again.
            if self.ap_should_stop():
                self.ap_trace('limit_stop', ap_limit=self.conf.general_climb.ap_limit)
                return None
            if self.conf.general_climb.five_times_enable:
                raise ApSafetyStop('Five-times configuration changed during readiness')
            target = self.I_AP_GUJI_UNLOCK if self.conf.general_battle.enable_ap_preset else self.I_AP_GUJI_LOCK
            opposite = self.I_AP_GUJI_LOCK if self.conf.general_battle.enable_ap_preset else self.I_AP_GUJI_UNLOCK
            ready = (state == ApState.CHALLENGE and self.ap_x5_state() == X5State.OFF
                     and self.ap_match(target) and not self.ap_match(opposite)
                     and all(self.ap_match(rule) for rule in (
                         self.I_AP_GUJI_FIRE, self.I_AP_GUJI_AP_COST,
                         self.I_AP_GUJI_ACTIVITY_COST)))
            if ready:
                roi = tuple(self.I_AP_GUJI_FIRE.roi_front)
                x, y, w, h = roi
                patch = self.device.image[y:y+h, x:x+w].copy()
                key = self.ap_session_key()
                same = (anchor_roi is not None and key == anchor_config
                        and max(abs(a-b) for a,b in zip(roi,anchor_roi)) <= 2
                        and patch.shape == anchor_patch.shape
                        and np.abs(patch.astype(float)-anchor_patch.astype(float)).mean() <= 12)
                if same:
                    score = cv2.matchTemplate(patch,anchor_patch,cv2.TM_CCOEFF_NORMED)[0,0]
                    same = np.isfinite(score) and score >= .97
                if not same:
                    since, frames = monotonic(), 0
                    anchor_roi, anchor_patch, anchor_config = roi, patch, key
                frames += 1
                if frames >= 3 and monotonic()-since >= self.AP_CHALLENGE_STABLE:
                    self.ap_trace('challenge_ready', stable_seconds=round(monotonic()-since,3),
                                  frames=frames, fire_roi=roi, x5='OFF')
                    return state
            else:
                since, frames, anchor_roi, anchor_patch, anchor_config = None, 0, None, None, None
            sleep(self.AP_ENTRY_POLL)
        self.ap_invalidate_session()
        raise ApSafetyStop('Challenge controls did not become continuously stable')

    def ap_click_fire(self):
        if self.ap_state() != ApState.CHALLENGE or self.ap_x5_state() != X5State.OFF:
            raise ApSafetyStop('Challenge state changed before single input')
        if not self.ap_match(self.I_AP_GUJI_FIRE):
            raise ApSafetyStop('Challenge target lost before single input')
        x, y, w, h = self.I_AP_GUJI_FIRE.roi_front
        # A small rectangle around the text center, away from diamond edges.
        self.ap_click_rect((x+w//2-20,y+h//2-12,40,24),self.I_AP_GUJI_FIRE.name)

    def ap_start_battle(self):
        if getattr(self, '_ap_resource_exhausted', False):
            return False
        self.screenshot()
        state = self.ap_state()
        if state == ApState.CHALLENGE:
            if self.ap_insufficient():
                return False
            if self.ap_should_stop():
                self.ap_trace('limit_stop', ap_limit=self.conf.general_climb.ap_limit)
                return False
            if not self.check_tickets_enough():
                return False
            if self.ap_should_stop():
                return False
            state = self.ap_wait_challenge_ready()
            if state is None:
                return False
            # Recheck limits after all readiness frames. A changed session is
            # stopped here: don't change controls after the stability window.
            if state == ApState.CHALLENGE and getattr(self, '_ap_session_key', None) is not None:
                if not self.ap_session_matches():
                    self.ap_invalidate_session()
                    raise ApSafetyStop('Validated consumption changed during challenge readiness')
            if state == ApState.CHALLENGE and self.ap_should_stop():
                return False
            if self.conf.general_climb.five_times_enable:
                raise ApSafetyStop('Five-times configuration changed during preflight')
        elif state not in (ApState.BATTLE, ApState.PREPARE, ApState.VICTORY, ApState.REWARD):
            raise ApSafetyStop('Challenge button/state unavailable')
        entry_frame = None
        if state == ApState.CHALLENGE:
            self._ap_phase_seen = {}
            last_reward = getattr(self, '_ap_last_reward_click', None)
            self.ap_trace('fire_intent', reward_click_to_fire=(
                round(monotonic()-last_reward, 3) if last_reward is not None else 'first_entry'),
                ap_limit=self.conf.general_climb.ap_limit, limit_time=self.conf.general_climb.limit_time_v)
            entry_frame = self.device.image.copy()
            self.ap_click_fire()
            # This means the input backend returned, NOT a game acknowledgement.
            self.ap_trace('fire_delivered', game_acceptance='unconfirmed', attempt=1)
        self._ap_reward_recovery = state == ApState.REWARD
        started = monotonic()
        deadline = started+self.AP_ENTRY_TIMEOUT
        previous, diagnosed = None, False
        diagnostic_previous, diagnostic_stable = None, 0
        while monotonic() < deadline:
            self.screenshot()
            if self.ap_insufficient():
                return False
            state = self.ap_state()
            diagnostic_stable = diagnostic_stable+1 if state == diagnostic_previous else 1
            if state != diagnostic_previous:
                self.ap_diagnostic_trace('entry_state', elapsed=round(monotonic()-started, 3),
                                         state=state.value, confidence='not_exposed_boolean_classifier',
                                         stable_count=diagnostic_stable, phase='change')
                diagnostic_previous = state
            if state != previous:
                self.ap_trace('entry_observed', state=state.value, elapsed=round(monotonic()-started,3))
                previous = state
            if state in (ApState.BATTLE, ApState.PREPARE, ApState.VICTORY, ApState.REWARD):
                self.ap_observe_phase(state)
                self.ap_trace('entry_confirmed', state=state.value)
                return self.run_general_battle(config=self.get_general_battle_conf())
            if state in (ApState.DIALOG, ApState.FAILED):
                raise ApSafetyStop(f'Entry blocked by {state.value}; never confirm')
            if entry_frame is not None and not diagnosed and monotonic()-started >= 3:
                # Preserve both sides of an anomalously slow entry. Normal
                # rounds produce no extra files, OCR or screenshot calls.
                self.ap_snapshot('entry_before_input', image=entry_frame)
                self.ap_snapshot('entry_pending_3s')
                self.ap_trace('entry_pending', state=state.value, retry_allowed=False,
                              reason='unspent_resources_and_no_pending_request_unproven')
                self.ap_diagnostic_trace('entry_state', elapsed=round(monotonic()-started, 3),
                                         state=state.value, confidence='not_exposed_boolean_classifier',
                                         stable_count=diagnostic_stable, phase='pending_3s')
                diagnosed = True
            # Missing button / slow animation NEVER triggers a second click.
            sleep(self.AP_ENTRY_POLL)
        self.screenshot()  # Final takeover check after the entry deadline.
        if self.ap_insufficient():
            return False
        final_state = self.ap_state()
        diagnostic_stable = diagnostic_stable+1 if final_state == diagnostic_previous else 1
        self.ap_diagnostic_trace('entry_state', elapsed=round(monotonic()-started, 3),
                                 state=final_state.value, confidence='not_exposed_boolean_classifier',
                                 stable_count=diagnostic_stable, phase='deadline_frame')
        if final_state in (ApState.BATTLE, ApState.PREPARE, ApState.VICTORY, ApState.REWARD):
            self.ap_observe_phase(self.ap_state())
            return self.run_general_battle(config=self.get_general_battle_conf())
        self.ap_invalidate_session()
        self.ap_trace('entry_timeout', state=self.ap_state().value, retry_allowed=False,
                      elapsed=round(monotonic()-started,3))
        self.ap_diagnostic_trace('entry_state', elapsed=round(monotonic()-started, 3),
                                 state='timeout', observed_state=final_state.value,
                                 confidence='not_exposed_boolean_classifier', stable_count=diagnostic_stable,
                                 phase='timeout')
        self.ap_snapshot('entry_timeout')
        raise ApSafetyStop('Battle entry timed out; no second challenge click')

    def battle_before(self, buff, config, timeout=5):
        if not getattr(self, '_ap_guji_active', False):
            return super().battle_before(buff, config, timeout)
        if self.ap_state() in (ApState.VICTORY, ApState.REWARD):
            return True
        self._ap_guard_deadline = monotonic()+30
        try:
            result = super().battle_before(buff, config, timeout=25)
            # The parent ignores a False return; raise instead of continuing an
            # unconfirmed preset/preparation state into settlement.
            if not result and self.ap_state() not in (ApState.VICTORY, ApState.REWARD):
                raise ApSafetyStop('Preparation did not enter real battle')
            return result
        finally:
            self._ap_guard_deadline = None

    def ap_settlement_click(self, state):
        if state not in (ApState.VICTORY, ApState.REWARD) or self.ap_state() != state:
            raise ApSafetyStop('Settlement not positively identified')
        point = sample_settlement_point()
        self.ap_trace('settlement_intent', state=state.value)
        self.device.click(*point, control_name='AP_GUJI_SETTLEMENT',
                          control_roi=(*point, 1, 1))
        self.ap_trace('settlement_delivered', state=state.value)
        if state == ApState.REWARD:
            self._ap_last_reward_click = monotonic()

    def ap_battle_wait(self):
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()  # Same per-battle boundary as legacy wait.
        deadline = monotonic()+self.AP_BATTLE_TIMEOUT
        previous, stable, last_click = None, 0, -100.0
        last_clicked_state = None
        phase_times, unknown_since = {}, None
        attempts = {ApState.VICTORY: 0, ApState.REWARD: 0}
        saw_victory = saw_reward = False
        diagnostic_after_click_state = None
        while monotonic() < deadline:
            self.screenshot()
            state = self.ap_state()
            if state in (ApState.DIALOG, ApState.FAILED):
                raise ApSafetyStop(f'Battle/settlement interrupted by {state.value}')
            if state == previous:
                stable += 1
            else:
                previous, stable = state, 1
            if last_clicked_state == ApState.REWARD:
                self.ap_diagnostic_trace('settlement_after_click', click_count=attempts[ApState.REWARD],
                                         click_time=round(last_click, 6),
                                         click_time_source='post_backend_return_boundary',
                                         elapsed_after_click=round(monotonic()-last_click, 3),
                                         current_state=state.value, stable_count=stable,
                                         state_changed=state != diagnostic_after_click_state,
                                         confidence='not_exposed_boolean_classifier')
                diagnostic_after_click_state = state
            observed = self.ap_observe_phase(state)
            if state in observed:
                phase_times.setdefault(state, observed[state])
            if state == ApState.UNKNOWN:
                unknown_since = unknown_since if unknown_since is not None else monotonic()
                if monotonic()-unknown_since > self.AP_UNKNOWN_TIMEOUT:
                    raise ApSafetyStop('Battle/settlement state unknown for too long')
            else:
                unknown_since = None
            if state in (ApState.VICTORY, ApState.REWARD):
                if state == ApState.VICTORY:
                    if saw_reward:
                        raise ApSafetyStop('Unexpected return from reward to victory')
                    saw_victory = True
                else:
                    # Recovery can start at an already displayed reward, but
                    # completion still requires a confirmed challenge return.
                    if not saw_victory and not getattr(self, '_ap_reward_recovery', False):
                        raise ApSafetyStop('Reward appeared without a confirmed victory')
                    saw_reward = True
                if monotonic()-phase_times[state] > self.AP_SETTLE_TIMEOUT:
                    raise ApSafetyStop('Settlement phase timed out')
                gap = self.AP_RETRY_CLICK_GAP if last_clicked_state == state else self.AP_STAGE_CLICK_GAP
                if stable >= 2 and monotonic()-last_click >= gap:
                    if attempts[state] >= 3:
                        raise ApSafetyStop('Settlement unchanged after three single clicks')
                    self.ap_settlement_click(state)
                    attempts[state] += 1
                    last_click = monotonic()
                    last_clicked_state = state
                    if state == ApState.REWARD:
                        diagnostic_after_click_state = None
                    # The next loop immediately captures AND classifies the
                    # post-click frame; no discarded screenshot or fixed sleep.
                    continue
            elif state == ApState.CHALLENGE:
                if saw_reward and stable >= 2:
                    self.count_map['ap'] = self.current_count
                    self._ap_completed = getattr(self, '_ap_completed', 0)+1
                    self.ap_trace('settlement_complete',
                                  victory_to_return=round(monotonic()-phase_times.get(ApState.VICTORY, monotonic()), 3),
                                  reward_click_to_return=round(monotonic()-last_click, 3))
                    return True
                if not saw_reward:
                    raise ApSafetyStop('Returned to challenge without confirmed reward settlement')
            elif state not in (ApState.BATTLE, ApState.PREPARE, ApState.UNKNOWN):
                raise ApSafetyStop(f'Unexpected settlement destination: {state.value}')
            sleep(.5 if state in (ApState.BATTLE, ApState.PREPARE) else self.AP_SETTLE_POLL)
        raise ApSafetyStop('Battle or settlement timed out')

    def ap_loop(self):
        self.ap_navigate()
        while True:
            # Navigation and settlement both leave a fresh confirmed frame.
            # ap_start_battle owns the single fresh preflight and session check.
            state = self.ap_state()
            if state in (ApState.BATTLE, ApState.PREPARE, ApState.VICTORY, ApState.REWARD):
                self.start_battle()  # Finish an already started battle even at limits.
                continue
            if state != ApState.CHALLENGE:
                raise ApSafetyStop('Lost challenge page between battles')
            if self.ap_should_stop():
                self.ap_trace('limit_stop', ap_limit=self.conf.general_climb.ap_limit)
                return
            if not self.start_battle():
                return
