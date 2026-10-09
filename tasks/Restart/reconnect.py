"""Instance-local takeover gate; scheduling and login remain in Script / Restart."""
import re
import json
import math
import time
from datetime import datetime, timedelta

from module.exception import GameStuckError, RequestHumanTakeover
from module.logger import logger


class ReconnectInterrupted(BaseException):
    """Unwind the task, including its ordinary `except Exception` handlers.

    This is scheduler control flow, like cancellation, not a task failure that
    a subtask may consume and mark completed.
    """


TARGET = '检测到账号在其他设备登录'
ROI = (460, 290, 360, 65)
GAME_LOGIN_ROI = (550, 567, 176, 56)
GAME_LOGIN_AREA = (558, 574, 154, 49)


def match_takeover(text, score):
    normal = re.sub(r'[\s，。！？、：；,.!?:;]', '', text)
    normal = normal.replace('帐号', '账号').replace('登錄', '登录')
    return normal == TARGET and score >= 0.95


class ReconnectGate:
    MAX_ATTEMPTS = 2
    SCAN_INTERVAL = 2.0
    CONFIRM_INTERVAL = 0.3

    def __init__(self, config, instance, *, clock=time.monotonic,
                 wall_clock=datetime.now, sleep=time.sleep, ocr=None):
        self.config = config  # getter: configuration may be replaced at a checkpoint
        self.instance = instance
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.ocr = ocr
        self.task = ''
        self.pending = False
        self.state = 'NORMAL'
        self.attempts = 0
        self.deadline = None
        self.detected_at = None
        self.error = ''
        self.last_scan = float('-inf')
        self.last_frame = float('-inf')
        self.last_region = None
        self.recognized = None
        self.desktop_staged = False
        self.android_staged = False
        self.checkpoint = lambda: None
        self.login_allowed = lambda: True
        self.entry_check = None
        self.entry_candidate = None
        self.game_entry_at = None
        self.due_at = None
        self.android_login = None
        self._android_log_key = None
        self._android_takeover_at = None
        self._android_confirming_takeover = False

    def enabled(self):
        group = getattr(self.config().restart, 'auto_reconnect_config', None)
        return bool(group and group.auto_reconnect_enable)

    def eligible(self):
        # Bound to the outer Script.run, never to BaseTask / an adapter's name.
        return bool(self.task and not self.task.startswith('Multi')
                    and self.task not in {'Restart', 'GotoMain', 'AutoCheckinBigGod', 'SwitchAccount'})

    def delay_minutes(self):
        config = self.config()
        override = (config.kekkai_utilize.utilize_config.foster_reconnect_delay_minutes
                    if self.task == 'KekkaiUtilize' else 0)
        return override or config.restart.auto_reconnect_config.auto_reconnect_delay_minutes

    def scan(self, image):
        if image is None or image.shape[:2] != (720, 1280):
            raise RequestHumanTakeover('Reconnect requires a normalized 1280x720 screenshot')
        if self.ocr is None:
            from module.ocr.models import get_ocr_model
            self.ocr = get_ocr_model('ch').ocr_single_line
        x, y, w, h = ROI
        try:
            text, score = self.ocr(image[y:y+h, x:x+w])
            logger.debug(f'[Reconnect OCR] instance={self.instance} text={text!r} score={score:.5f}')
        except Exception as error:
            logger.error(f'[Reconnect OCR] instance={self.instance} error={error}')
            raise RequestHumanTakeover('Reconnect OCR unavailable; no game input allowed') from error
        if match_takeover(text, 1.0) and score < 0.95:
            raise RequestHumanTakeover(f'Takeover text has low confidence: {score:.5f}')
        self.last_scan = self.clock()
        self.last_region = self.region_signature(image)
        self.recognized = (text, score)
        return match_takeover(text, score)

    @staticmethod
    def region_signature(image):
        x, y, w, h = ROI
        return image[y:y+h, x:x+w].tobytes()

    def observe(self, device, *, force=False):
        self.last_frame = self.clock()
        if self.android_staged and self._android_confirming_takeover:
            return
        if self.pending and self.state == 'RECONNECTING' and not self.enabled():
            self.fail('Automatic reconnect disabled by user')
        if self.state in {'WAITING', 'VERIFY', 'FAILED', 'PREPARE_LOGIN', 'WAIT_AT_GAME_LOGIN'}:
            return
        if not self.enabled() or not self.eligible():
            return
        if not force and self.state != 'SUSPECTED' and self.clock() - self.last_scan < self.SCAN_INTERVAL:
            return
        try:
            hit = self.scan(device.image)
        except RequestHumanTakeover as error:
            self.fail(str(error))
        if self.state == 'SUSPECTED':
            self.state = 'RECOVERED' if self.pending else 'NORMAL'
            if hit:
                self.pending = True
                self.state = 'WAITING'
                self.detected_at = self.wall_clock()
                minutes = self.delay_minutes()
                self.deadline = self.clock() + minutes * 60
                self.due_at = self.detected_at + timedelta(minutes=minutes)
                logger.warning(f'[Reconnect] instance={self.instance} task={self.task} '
                               f'confirmed={self.detected_at} due={self.detected_at + timedelta(minutes=minutes)} '
                               f'ocr={self.recognized!r}')
                raise ReconnectInterrupted()
            return
        if hit:
            if self.state == 'RECONNECTING':
                if self.android_staged:
                    # A new confirmed takeover cannot spend the old, expired due.
                    self.checkpoint()
                    self.sleep(self.CONFIRM_INTERVAL)
                    self._android_confirming_takeover = True
                    try:
                        device.screenshot()
                    finally:
                        self._android_confirming_takeover = False
                    self.checkpoint()
                    if self.scan(device.image):
                        self.android_login = dict(status='TAKEOVER', reason='takeover_confirmed_again',
                                                  template=None, ocr_status='not_run', ocr_text=None,
                                                  ocr_confidence=None, role_markers=[], blocking_markers=[],
                                                  takeover=True)
                        self.fail('Takeover confirmed again during Android reconnect; old deadline unusable')
                    return
                # Existing Login retry boundary consumes this failure, not Scheduler.
                raise GameStuckError('Takeover popup reappeared during reconnect')
            self.state = 'SUSPECTED'
            # Only a suspicious frame pays this small confirmation delay. No input
            # is returned to the task between the two fresh screenshots.
            self.sleep(self.CONFIRM_INTERVAL)
            device.screenshot()

    def guard_input(self, device):
        if (self.desktop_staged or self.android_staged) and self.pending:
            self.checkpoint()
            if self.state == 'RECONNECTING' and (self.clock() < self.deadline or not self.login_allowed()):
                raise ReconnectInterrupted()
        if self.pending:
            if self.state not in {'RECONNECTING', 'RECOVERED'} or not self.enabled():
                raise ReconnectInterrupted()
        if self.enabled() and self.eligible():
            # Refresh after pacing / a long computation; never re-use a stale
            # task screenshot at the final input dispatch (including pace=False).
            if self.clock() - self.last_frame >= self.CONFIRM_INTERVAL:
                device.screenshot()
            # A throttled screenshot may already contain a new popup. Before
            # clicking, cheaply compare ONLY the text ROI with the last OCR'd
            # ROI; changed text must be checked even inside the 2s throttle.
            if self.region_signature(device.image) != self.last_region:
                self.observe(device, force=True)

    def game_login_text(self, image):
        if image is None or image.shape[:2] != (720, 1280):
            raise RequestHumanTakeover('Game login requires a normalized 1280x720 frame')
        if self.ocr is None:
            from module.ocr.models import get_ocr_model
            self.ocr = get_ocr_model('ch').ocr_single_line
        x, y, w, h = GAME_LOGIN_ROI
        try:
            text, score = self.ocr(image[y:y+h, x:x+w])
        except Exception as error:
            logger.error(f'[Game login OCR] instance={self.instance} error={error}')
            raise RequestHumanTakeover('Game-entry OCR unavailable') from error
        logger.debug(f'[Game login OCR] instance={self.instance} text={text!r} score={score:.5f}')
        return re.sub(r'\s', '', text) == '进入游戏' and score >= 0.95

    def inspect_android_login(self, image, *, template, role_markers, blocking_markers):
        """Read-only Android observation; an ordinary missing entry is not danger."""
        result = dict(status='UNCONFIRMED', reason='template_missing', template=bool(template),
                      ocr_status='not_run', ocr_text=None, ocr_confidence=None,
                      role_markers=list(role_markers), blocking_markers=list(blocking_markers),
                      takeover=False, takeover_ocr_status='not_run')
        self.android_login = result
        try:
            if image is None or image.shape[:2] != (720, 1280):
                raise RequestHumanTakeover('Game login requires a normalized 1280x720 frame')
            if role_markers:
                result.update(status='DANGER', reason='role_before_entry')
                raise RequestHumanTakeover('Android entered a role before the verified entry press; '
                                           'possible early auto-login')
            try:
                hit = self.scan(image)
            except Exception:
                result['takeover_ocr_status'] = 'error'
                raise
            result['takeover_ocr_status'] = 'ok'
            result['takeover'] = hit
            if hit:
                now = self.clock()
                if self._android_takeover_at is not None and now - self._android_takeover_at >= self.CONFIRM_INTERVAL:
                    result.update(status='TAKEOVER', reason='takeover_confirmed_again')
                    raise RequestHumanTakeover('Takeover confirmed again during Android wait; old deadline unusable')
                if self._android_takeover_at is None:
                    self._android_takeover_at = now
                result['reason'] = 'takeover_suspected'
                return False
            self._android_takeover_at = None
            if blocking_markers:
                result['reason'] = 'popup_or_sdk_visible'
                return False
            if not template:
                return False
            x, y, w, h = GAME_LOGIN_ROI
            result['ocr_status'] = 'running'
            text, score = self.ocr(image[y:y+h, x:x+w])
            confidence = float(score)
            result.update(ocr_status='ok', ocr_text=text,
                          ocr_confidence=confidence if math.isfinite(confidence) else None)
            if re.sub(r'\s', '', text) != '进入游戏':
                result['reason'] = 'entry_text_mismatch'
                return False
            if not math.isfinite(confidence) or confidence < 0.95:
                result['reason'] = 'entry_low_confidence'
                return False
            result.update(status='READY', reason='entry_confirmed')
            return True
        except Exception as error:
            if result['status'] not in {'DANGER', 'TAKEOVER'}:
                result.update(status='ERROR', reason=str(error))
                if result['ocr_status'] == 'running':
                    result['ocr_status'] = 'error'
            if isinstance(error, RequestHumanTakeover):
                raise
            raise RequestHumanTakeover('Android game-entry OCR unavailable; no game input allowed') from error
        finally:
            self.log_android_login()

    def log_android_login(self, event='observation', *, force=False):
        """Only state/reason changes plus due/termination/actual dispatch are logged."""
        if not self.android_staged:
            return
        observation = self.android_login or dict(status='UNCONFIRMED', reason='not_observed',
                                                 template=None, ocr_status='not_run',
                                                 ocr_text=None, ocr_confidence=None,
                                                 role_markers=[], blocking_markers=[])
        key = (self.state, observation['status'], observation['reason'], event)
        if not force and key == self._android_log_key:
            return
        self._android_log_key = key
        now = self.clock()
        payload = dict(instance=self.instance, task=self.task, stage=self.state, event=event,
                       due=self.due_at.isoformat() if self.due_at else None,
                       monotonic=round(now, 3), deadline=self.deadline,
                       remaining=round(max(0, self.deadline-now), 3) if self.deadline is not None else None,
                       **observation, terminal_error=self.error or None,
                       entry_sent=self.game_entry_at is not None)
        # Only fixed entry ROI text is included; no account list or full-screen OCR.
        logger.info('[Android reconnect] ' + json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def guard_game_entry(self, device, point):
        if (not (self.desktop_staged or self.android_staged) or not self.pending or self.state != 'RECONNECTING'
                or self.game_entry_at is not None):
            return
        self.guard_input(device)
        x, y, w, h = GAME_LOGIN_AREA
        if not (x <= point[0] < x+w and y <= point[1] < y+h):
            raise RequestHumanTakeover('First game input must be the verified second-layer entry')
        self.entry_candidate = point

    def before_game_entry(self, device):
        if (not (self.desktop_staged or self.android_staged) or not self.pending or self.state != 'RECONNECTING'
                or self.game_entry_at is not None):
            return
        self.guard_input(device)
        if self.entry_candidate is None or self.entry_check is None or not self.entry_check():
            raise RequestHumanTakeover('Second-layer game login changed before input')
        self.checkpoint()
        if self.clock() < self.deadline or not self.login_allowed():
            raise ReconnectInterrupted()
        return True

    def record_game_entry(self):
        self.game_entry_at = self.wall_clock()
        logger.info(f'[Reconnect] instance={self.instance} actual_game_entry={self.game_entry_at} '
                    f'monotonic={self.clock():.3f} deadline={self.deadline:.3f}')

        if self.android_staged:
            self.log_android_login('entry_sent', force=True)

    def before_attempt(self):
        if not self.enabled():
            self.fail('Automatic reconnect disabled by user')
        if self.attempts >= self.MAX_ATTEMPTS:
            raise RequestHumanTakeover('Automatic reconnect login budget exhausted (2)')
        self.attempts += 1
        logger.info(f'[Reconnect] instance={self.instance} login attempt={self.attempts}/2')

    def fail(self, message):
        self.error, self.pending, self.state = message, True, 'FAILED'
        raise ReconnectInterrupted()

    def complete_task(self):
        self.pending, self.state, self.attempts = False, 'NORMAL', 0
        self.deadline = None
        self.error = ''
        self.desktop_staged = False
        self.android_staged = False
        self.entry_candidate = self.game_entry_at = self.entry_check = None
        self.due_at = self.android_login = self._android_log_key = self._android_takeover_at = None
