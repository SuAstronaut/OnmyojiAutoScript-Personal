"""Bounded return navigation. No device/session state is cached between calls."""
import copy
import json
import re
import time
import uuid
from pathlib import Path

from module.atom.image import RuleImage
from module.exception import GameNotRunningError, RequestHumanTakeover
from module.logger import logger


class ReturnTrace:
    """Keep a small in-memory trace; write only on a conflict or failed recovery.

    Never OCR for diagnostics. Probe copies so diagnostic matches cannot change
    the coordinates used by the actual action. Central account fields and the
    courtyard nickname are redacted from saved frames without reading text.
    """
    def __init__(self, task, stage):
        self.task = task
        self.stage = stage
        self.instance = re.sub(r'[^\w-]', '_', str(task.config.config_name))[:40]
        self.started = time.monotonic()
        self.last_event_at = self.started
        self.events = []
        self.folder = None
        self.snapshots = 0
        self.input_frame = None  # 引用已有帧；正常流程不复制、不额外截图、不落盘。

    def note(self, result, **fields):
        now = time.monotonic()
        self.events.append(dict(elapsed=round(now - self.started, 3),
                                step_s=round(now - self.last_event_at, 3),
                                result=result, **fields))
        self.last_event_at = now
        self.events = self.events[-32:]

    def capture(self, result, *, image=None):
        if self.snapshots >= 3:
            return
        try:
            import cv2
            from tasks.GameUi.page import page_main, page_theme, page_login, page_team
            rules = list(page_main.check_button) + list(page_theme.check_button)
            for name in ('I_UI_BACK_RED', 'I_UI_BACK_YELLOW', 'I_BACK_BLACK',
                         'I_BACK_MAIN', 'I_SA_USER_CENTER_PROFILE', 'I_SA_USER_CENTER',
                         'I_SA_SWITCH_ACCOUNT_BTN', 'I_NETWORK_ERROR', 'I_NETWORK_ABNORMAL'):
                rule = getattr(self.task, name, None)
                if isinstance(rule, RuleImage):
                    rules.append(rule)
            if image is None:
                image = self.task.device.image
            scores = []
            for rule in rules:
                probe = copy.copy(rule)
                probe.roi_front = list(rule.roi_front)
                matched = probe.match(image)
                score = getattr(probe, 'last_match_score', None)
                scores.append(dict(template=probe.name, matched=bool(matched),
                                   score=float(score) if score is not None else None,
                                   threshold=probe.threshold, roi=list(probe.roi_back)))
            pages = []
            for page in (page_main, page_theme, page_login, page_team):
                checks = page.check_button if isinstance(page.check_button, list) else [page.check_button]
                hits = []
                for rule in checks:
                    if isinstance(rule, RuleImage):
                        probe = copy.copy(rule)
                        probe.roi_front = list(rule.roi_front)
                        matched = bool(probe.match(image))
                        score = getattr(probe, 'last_match_score', None)
                        hits.append(dict(template=probe.name, matched=matched,
                                         score=float(score) if score is not None else None,
                                         threshold=probe.threshold))
                pages.append(dict(page=str(page), matched=any(hit['matched'] for hit in hits),
                                  checks=hits,
                                  navigation_only=getattr(page, 'navigation_only', False)))
            if self.folder is None:
                self.folder = (Path('log/screenshots/navigation_return') / self.instance
                               / uuid.uuid4().hex)
                self.folder.mkdir(parents=True, exist_ok=True)
            self.snapshots += 1
            image = image.copy()
            image[0:125, 90:350] = 0  # 庭院角色名。
            image[160:620, 180:1110] = 0  # 邮箱/账号/昵称所在的表单中央。
            ok, encoded = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
            if ok:
                (self.folder / f'{self.snapshots:02d}_{result}.png').write_bytes(encoded.tobytes())
            payload = dict(instance=self.instance, stage=self.stage, result=result,
                           current_page=str(getattr(self.task, 'ui_current', None)),
                           candidates=scores, pages=pages, events=self.events,
                           redacted=True)
            (self.folder / f'{self.snapshots:02d}_{result}.json').write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.warning(f'[RETURN_DIAG] stage={self.stage} result={result} path={self.folder}')
        except Exception as error:
            # 归档失败不能掩盖原始恢复结果；不打印异常内容中的潜在账号信息。
            logger.warning(f'[RETURN_DIAG] save_failed type={type(error).__name__}')

    def finish(self, result):
        self.note(result)
        if result != 'arrived' and self.input_frame is not None:
            self.capture('before_last_input', image=self.input_frame)
        if self.folder is not None or result != 'arrived':
            self.capture(result)


class ReturnMainMixin:
    def ui_traced_click(self, target, interval, trace):
        """Keep input cooldowns/RNG/humanizer intact; observe coordinates lazily."""
        operation = copy.copy(target)
        coordinates = {}

        def coord():
            x, y = target.coord()
            coordinates.update(x=int(x), y=int(y))
            trace.input_frame = self.device.image
            return x, y

        operation.coord = coord
        page = str(getattr(self, 'ui_current', None))
        try:
            sent = self.click(operation, interval=interval)
        except Exception as error:
            trace.note('input_error', action=target.name, page=page,
                       error_type=type(error).__name__, **coordinates)
            trace.finish('input_error')
            raise
        finally:
            self.ui_current = None
        trace.note('input_sent' if sent else 'input_not_sent',
                   action=target.name, page=page, **coordinates)
        return sent

    def ui_peek_page(self):
        """Read the current frame once, without capture, OCR fallback or recovery.

        Explicit ui_page_appear(page_theme) and the navigation graph are unchanged.
        Global identification must not promote a transit icon into a real page.
        """
        for page in self.ui_pages:
            if not page.check_button or getattr(page, 'navigation_only', False):
                continue
            if self._match_page(page):
                return page
        return None

    def ui_main_confirmed(self):
        """A fresh-frame courtyard predicate; an open red-close panel vetoes it.

        Yellow/black arrows must never override a confirmed courtyard and cause
        another click in the avatar region. No template/threshold is changed.
        """
        from tasks.GameUi.page import page_main
        from tasks.Component.SwitchAccount.assets import SwitchAccountAssets as SA
        return (self._match_page(page_main)
                and self._current_overlay_page() is None
                and not self.appear(self.I_NETWORK_ERROR)
                and not self.appear(self.I_NETWORK_ABNORMAL)
                and not self.appear(self.I_UI_BACK_RED)
                and not self.appear(SA.I_SA_SWITCH_ACCOUNT_BTN)
                and not (self.appear(SA.I_SA_USER_CENTER_PROFILE)
                         and self.appear(SA.I_SA_USER_CENTER)))

    def ui_return_to_main(self, stage, *, timeout=30, max_actions=6,
                          skip_first_screenshot=False, accept_login=False,
                          extra_back=()):
        """One total budget for identification, matched actions and confirmation.

        30s allows six returns with a shared 2.5s settle/click interval. Historic
        successful return frames took ~1.8s under three instances. Missing inputs
        do not start arrival waits. Unknown screens get only matched safe backs,
        never random points, general confirms, or a fresh nested timeout.
        """
        from tasks.GameUi.page import page_main, page_login
        trace = ReturnTrace(self, stage)
        deadline = time.monotonic() + timeout
        actions = 0
        next_action = 0
        last_progress = time.monotonic()
        conflict_saved = False
        while time.monotonic() < deadline:
            self.maybe_screenshot(skip_first_screenshot)
            skip_first_screenshot = False
            # Invalidate even a cached main after every new frame and input.
            self.ui_current = None
            if time.monotonic() >= deadline:
                break
            if self.appear(self.I_NETWORK_ERROR) or self.appear(self.I_NETWORK_ABNORMAL):
                trace.finish('network_blocked')
                raise RequestHumanTakeover('Network dialog blocks return; see RETURN_DIAG')
            if self.ui_main_confirmed():
                self.ui_current = page_main
                trace.finish('arrived')
                return True
            # Normal returns need only overlay/courtyard/login and matched backs.
            # Do not scan every registered callable/OCR page on every frame.
            page = self._current_overlay_page()
            self.ui_current = page
            account_form = (accept_login
                            and self.appear(self.I_SA_NETEASE_GAME_LOGO)
                            and self.appear(self.I_SA_ACCOUNT_LOGIN_BTN))
            if self._match_page(page_login) or account_form:
                if accept_login:
                    self.ui_current = page_login
                    trace.finish('arrived')
                    return True
                trace.finish('unexpected_login')
                raise GameNotRunningError('Login encountered during return')
            if time.monotonic() - last_progress >= 6:
                trace.note('no_progress', page=str(page))
                if not conflict_saved:
                    trace.capture('conflict')
                    conflict_saved = True
            # Overlay actions are allowed only after that specific overlay was
            # identified. Do not use the global unknown-page CONFIRM action list.
            candidates = []
            if page is not None and page.overlay:
                candidates.extend(page.additional or ())
                candidates.append(page.links.get(page_main))
            candidates.extend((self.I_UI_BACK_RED, self.I_BACK_MAIN, *extra_back))
            if time.monotonic() < next_action:
                continue
            if actions >= max_actions:
                trace.finish('action_limit')
                raise RequestHumanTakeover('Return action limit; see RETURN_DIAG')
            for target in candidates:
                if time.monotonic() >= deadline:
                    break
                if not isinstance(target, RuleImage) or not self.appear(target):
                    continue
                sent = self.ui_traced_click(target, 2.5, trace)
                if sent:
                    actions += 1
                    next_action = time.monotonic() + 2.5
                    last_progress = time.monotonic()
                # Next loop always confirms a new frame, including a failed input.
                break
        self.ui_current = None
        trace.finish('timeout')
        raise RequestHumanTakeover('Return total budget exhausted; see RETURN_DIAG')
