# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import cv2
import os
from time import sleep, time

import random
from datetime import datetime, time as datetime_time, timedelta
from module.atom.animate import RuleAnimate
from module.atom.click import RuleClick
from module.atom.gif import RuleGif
from module.atom.image import RuleImage
from module.atom.list import RuleList
from module.atom.long_click import RuleLongClick
from module.atom.ocr import RuleOcr
from module.atom.swipe import RuleSwipe
from module.atom.input import RuleInput  # 新增导入
from module.base.timer import Timer
from module.config.config import Config
from module.device.device import Device
from module.exception import ScriptError, TaskEnd
from module.config.utils import convert_to_underscore, forbidden_range_end
from module.logger import logger
from module.ocr.base_ocr import OcrMode
from tasks.Component.Costume.costume_base import CostumeBase
from tasks.Component.config_base import Time
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.GlobalGame.config_emergency import FriendInvitation
from typing import Union
from enum import Enum
from pydantic import Field
from tasks.Component.config_base import ConfigBase



class Week(str, Enum):
            mon = '周一'
            tue = '周二'
            wed = '周三'
            thu = '周四'
            fri = '周五'
            sat = '周六'
            sun = '周日'


class SwitchWeek(ConfigBase):
    next_week_day: Week = Field(default=Week.mon, description='选择下周周几运行')


def _identity(value):
    """原样透传：配置字段类型与任务属性类型一致时使用。"""
    return value


def _time_to_timedelta(value):
    """把 Time(datetime.time) 换算成 timedelta，与各任务 run() 里的既有换算一致。

    已经是 timedelta 的原样返回（HeroTest 的条件换算会出现这种情况）。
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, datetime_time):
        return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)
    return value


# HOT 重绑规则表：把当前任务配置子树里变化的叶子字段同步回任务实例上的捕获标量。
#
# 为什么需要它：HOT 提交是原地改模型对象，所有捕获「子模型对象」的读法自动生效；
# 但 run() 开头 `self.limit_count = <配置值>` 这类把标量拷到实例属性的写法拷的是值，
# 模型改了也追不回来（规格 §11.1「已复制到局部 scalar 的值不追溯更新」）。
# limit_time / limit_count 本就声明在 BaseTask 上（见下方类属性），属基类既有约定，
# 因此这张表放基类，13 个战斗任务无需各自实现 prepare hook。
#
# 表项：配置叶子字段名 -> (任务属性名, 值转换器)
_HOT_SCALAR_REBIND: dict = {
    'limit_count': ('limit_count', _identity),
    'hya_limit_count': ('limit_count', _identity),   # Hyakkiyakou 的源字段名不同
    'limit_time': ('limit_time', _time_to_timedelta),
}


class BaseTask(GlobalGameAssets, CostumeBase):
    config: Config = None
    device: Device = None

    folder: str
    name: str
    stage: str

    limit_time: timedelta = None  # 限制运行的时间，是软时间，不是硬时间
    limit_count: int = None  # 限制运行的次数
    current_count: int = None  # 当前运行的次数

    # 声明允许 HOT 提交替换的派生属性，框架据此拒绝 prepare 返回未声明字段（规格 §11.1）
    HOT_RELOAD_DERIVED_FIELDS = frozenset({'limit_count', 'limit_time'})

    def prepare_config_reload(self, candidate, changed_paths) -> dict:
        """HOT 两阶段提交的 prepare hook：算出捕获标量的新值，不做任何写入。

        必须是纯函数（规格 §11.1）：只读 candidate 与自身属性，返回新值由框架在
        RLock 内统一 setattr，因此不修改 task/session/device，也不做 I/O。

        三重限定，逐条都有存在必要：
        1. **任务子树限定**——只接受路径首段等于当前运行任务的配置键。否则用户改
           Orochi 的 limit_count 会把正在跑的 EvoZone 的次数上限改掉。
        2. **出处校验**——只有实例属性当前值恰好等于「旧模型值」时才重绑，证明它确实
           是从该字段拷来的且未被业务改写。这一条同时保护了 BondlingFairyland 的
           `self.limit_count //= 2`（handoff 模式两人各刷一半）：折半后值已不等于配置
           原值，不会被覆盖回去。
        3. **类型转换**——limit_time 在配置里是 Time，在任务里是 timedelta，
           必须按同一换算比较与回写，否则会塞进 time 对象让后续时间比较抛 TypeError。
        """
        # 注意：此时会话模型尚未提交，self.config.model 仍是旧值，正好用来做出处校验
        task_key = convert_to_underscore(self.config.task) if self.config.task else None
        if not task_key:
            return {}
        prepared: dict = {}
        for path in changed_paths:
            if path[0] != task_key:
                continue
            rule = _HOT_SCALAR_REBIND.get(path[-1])
            if rule is None:
                continue
            attr_name, convert = rule
            try:
                old_value = self.config._model_get(self.config.model, path)
                new_value = self.config._model_get(candidate, path)
            except AttributeError:
                # 路径与模型结构不匹配：交给 WARM 边界重建，不猜测
                continue
            if convert(old_value) != getattr(self, attr_name, None):
                # 出处校验未通过：该属性不是这个字段的原样拷贝，不动它
                continue
            prepared[attr_name] = convert(new_value)
        return prepared

    def __init__(self, config: Config, device: Device) -> None:
        """

        :rtype: object
        """
        self.config = config
        self.device = device

        # 初始化RuleInput实例
        self.rule_input = RuleInput(device)

        self.interval_timer = {}  # 这个是用来记录每个匹配的运行间隔的，用于控制运行频率
        self.animates = {}  # 保存缓存
        self.start_time = datetime.now()  # 启动的时间
        self.check_costume(self.config.global_game.costume_config)
        # self.friend_timer = None  # 这个是用来记录勾协的时间的
        # if self.config.global_game.emergency.invitation_detect_interval:
        #     self.interval_time = self.config.global_game.emergency.invitation_detect_interval
        #     self.friend_timer = Timer(self.interval_time)
        #     self.friend_timer.start()

        # 战斗次数相关
        self.current_count = 0  # 战斗次数
        self._boss_mark_flag = False

    def _burst(self) -> bool:
        """
        游戏界面突发异常检测
        :return: 没有出现返回False, 其他True
        """
        image = self.device.image
        appear_invitation = self.appear(self.I_G_ACCEPT)
        if not appear_invitation:
            return False
        logger.info('Invitation appearing')
        invite_type = self.config.global_game.emergency.friend_invitation
        detect_record = self.device.detect_record
        match invite_type:
            case FriendInvitation.ACCEPT:
                logger.info(f"Accept friend invitation")
                click_button = self.I_G_ACCEPT
            case FriendInvitation.REJECT:
                logger.info(f"Reject friend invitation")
                click_button = self.I_G_REJECT
            case FriendInvitation.ONLY_JADE:
                # 勾协
                logger.info(f"Only accept jade invitation")
                if self.appear(self.I_G_JADE):
                    click_button = self.I_G_ACCEPT
                else:
                    click_button = self.I_G_IGNORE
            case FriendInvitation.JADE_AND_FOOD:
                # 如果是接受勾协和粮协
                logger.info(f"Accept jade and food invitation")
                if self.appear(self.I_G_JADE) or self.appear(self.I_G_CAT_FOOD) or self.appear(self.I_G_DOG_FOOD):
                    click_button = self.I_G_ACCEPT
                else:
                    click_button = self.I_G_IGNORE
            case FriendInvitation.IGNORE:
                # 如果是忽略
                logger.info(f"Ignore friend invitation")
                click_button = self.I_G_IGNORE
            case _:
                raise ScriptError(f'Unknown friend invitation type: {invite_type}')
        if not click_button:
            raise ScriptError(f'Unknown click button type: {invite_type}')
        while 1:
            self.device.screenshot()
            if not self.appear(target=click_button):
                logger.info('Deal with invitation done')
                break
            if self.appear_then_click(click_button, interval=0.8):
                continue
        # 有的时候长战斗 点击后会取消战斗状态
        self.device.detect_record = detect_record
        # 如果接受邀请则立即执行悬赏任务
        if click_button == self.I_G_ACCEPT:
            self.set_next_run(task='WantedQuests', target=datetime.now().replace(microsecond=0))
        return True

    def screenshot(self):
        """
        截图 引入中间函数的目的是 为了解决如协作的这类突发的事件
        外层安全检查点：HOT 刷新位于最外层截图入口开始前（规格 §12）。
        _burst() 走的是 device.screenshot()，不经过本检查点，因此不存在嵌套刷新；
        Config 内的 _refresh_in_progress 只串行化并发 prepare，不覆盖 _burst() 执行期。
        生产默认 HOT 白名单为空，真实任务不发生中途替换。
        :return:
        """
        self.config.refresh_hot_at_checkpoint(self)
        self.device.screenshot()
        # 判断勾协
        self._burst()

        # # 判断网络异常
        # if self.appear(self.I_NETWORK_ABNORMAL):
        #     logger.warning(f"Network abnormal")
        #     raise GameStuckError
        #
        # # 判断网络错误
        # if self.appear(self.I_NETWORK_ERROR):
        #     logger.warning(f"Network error")
        #     raise GameStuckError

        return self.device.image

    def maybe_screenshot(self, soft_skip: bool = False):
        """
        可能截图
        :param soft_skip: True跳过截图(但保证设备一定有图才跳过,否则依然截图)
        :return:
        """
        if not soft_skip or not self.exist_image():
            return self.screenshot()
        return self.device.image

    def exist_image(self) -> bool:
        """
        判断当前设备是否有图片
        :return: 有返回True，没有返回False
        """
        return hasattr(self.device, 'image') and self.device.image is not None

    def appear(self,
               target: RuleImage | RuleGif | RuleOcr,
               interval: float = None,
               threshold: float = None):
        """

        :param target: 匹配的目标可以是RuleImage, 也可以是RuleOcr
        :param interval:
        :param threshold:
        :return: interval时间到达且匹配成功则返回True, 否则False
        """
        if interval:
            if target.name in self.interval_timer:
                if self.interval_timer[target.name].limit != interval:
                    self.interval_timer[target.name] = Timer(interval)
            else:
                self.interval_timer[target.name] = Timer(interval)
            if not self.interval_timer[target.name].reached():
                return False
        if isinstance(target, RuleOcr):
            appear = self.ocr_appear(target, interval)
        else:
            appear = target.match(self.device.image, threshold=threshold)

        if appear and interval:
            self.interval_timer[target.name].reset()

        return appear

    def appear_then_click(self,
                          target: RuleImage | RuleGif | RuleOcr,
                          action: Union[RuleClick, RuleLongClick] = None,
                          interval: float = None,
                          threshold: float = None,
                          duration: float = None,
                          repeat_exempt: bool = False):
        """
        出现了就点击，默认点击图片的位置，如果添加了click参数，就点击click的位置
        :param duration: 如果是长按，可以手动指定duration，不指定默认.单位是ms！！！！
        :param action: 可以是RuleClick, 也可以是RuleLongClick
        :param target: 可以是RuleImage后续支持RuleOcr
        :param interval:
        :param threshold:
        :param repeat_exempt: True 时本次点击豁免拟人化的同一资源连点退避——
            供「预期内连点」的资源在调用处显式声明（如十连召唤金按钮每出一抽
            重现一次，连点是流程的正常推进方式）；仅点击路径生效，长按不支持
        :return: True or False
        """
        appear = self.appear(target, interval=interval, threshold=threshold)
        if appear and not action:
            x, y = target.coord()
            self.device.click(x, y, control_name=target.name,
                              repeat_exempt=repeat_exempt)

        elif appear and action:
            x, y = action.coord()
            if isinstance(action, RuleLongClick):
                # action 的 roi_front 参与同一资源判别（点击区域与落点同源）
                if duration is None:
                    self.device.long_click(x, y, duration=action.duration / 1000,
                                           control_name=target.name, control_roi=action.roi_front)
                else:
                    self.device.long_click(x, y, duration=duration / 1000,
                                           control_name=target.name, control_roi=action.roi_front)
            elif isinstance(action, RuleClick):
                self.device.click(x, y, control_name=target.name, control_roi=action.roi_front,
                                  repeat_exempt=repeat_exempt)

        return appear

    def appear_multi_scale(self,
                           target: RuleImage,
                           interval: float = None,
                           threshold: float = None,
                           scales: list = None,
                           scale_range: tuple = None):
        """
        多尺度图片识别，自动尝试多个缩放比例以适应图片大小的变化
        :param target: RuleImage对象
        :param interval: 匹配间隔时间
        :param threshold: 匹配阈值
        :param scales: 缩放比例列表
        :param scale_range: 缩放范围 (start, end, step)，例如 (0.8, 1.2, 0.1)
        :return: interval时间到达且匹配成功则返回True, 否则False
        """
        if interval:
            if target.name in self.interval_timer:
                if self.interval_timer[target.name].limit != interval:
                    self.interval_timer[target.name] = Timer(interval)
            else:
                self.interval_timer[target.name] = Timer(interval)
            if not self.interval_timer[target.name].reached():
                return False

        appear = target.match_multi_scale(self.device.image, threshold=threshold, scales=scales, scale_range=scale_range)

        if appear and interval:
            self.interval_timer[target.name].reset()

        return appear

    def appear_then_click_multi_scale(self,
                                      target: RuleImage,
                                      action: Union[RuleClick, RuleLongClick] = None,
                                      interval: float = None,
                                      threshold: float = None,
                                      scales: list = None,
                                      scale_range: tuple = None,
                                      duration: float = None):
        """
        多尺度图片识别并点击，自动尝试多个缩放比例以适应图片大小的变化
        :param target: RuleImage对象
        :param action: 点击位置，可以是RuleClick或RuleLongClick
        :param interval: 匹配间隔时间
        :param threshold: 匹配阈值
        :param scales: 缩放比例列表
        :param scale_range: 缩放范围 (start, end, step)，例如 (0.8, 1.2, 0.1)
        :param duration: 长按时间（毫秒）
        :return: True or False
        """
        appear = self.appear_multi_scale(target, interval=interval, threshold=threshold, scales=scales, scale_range=scale_range)

        if appear and not action:
            x, y = target.coord()
            self.device.click(x, y, control_name=target.name)
        elif appear and action:
            x, y = action.coord()
            if isinstance(action, RuleLongClick):
                # action 的 roi_front 参与同一资源判别（点击区域与落点同源）
                if duration is None:
                    self.device.long_click(x, y, duration=action.duration / 1000,
                                           control_name=target.name, control_roi=action.roi_front)
                else:
                    self.device.long_click(x, y, duration=duration / 1000,
                                           control_name=target.name, control_roi=action.roi_front)
            elif isinstance(action, RuleClick):
                self.device.click(x, y, control_name=target.name, control_roi=action.roi_front)

        return appear

    def wait_until_appear(self,
                          target: RuleImage | RuleOcr,
                          skip_first_screenshot=False,
                          wait_time: int = None) -> bool:
        """
        等待直到出现目标
        :param wait_time: 等待时间，单位秒
        :param target:
        :param skip_first_screenshot:
        :return:
        """
        wait_timer = None
        if wait_time:
            wait_timer = Timer(wait_time)
            wait_timer.start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.screenshot()
            if wait_timer and wait_timer.reached():
                logger.warning(f"Wait until appear {target.name} timeout")
                return False
            if isinstance(target, RuleImage) and self.appear(target):
                return True
            if isinstance(target, RuleOcr) and self.ocr_appear(target):
                return True

    def wait_until_appear_then_click(self,
                                     target: RuleImage,
                                     action: Union[RuleClick, RuleLongClick] = None,
                                     wait_time: int = None) -> bool:
        """
        等待直到出现目标，然后点击
        :param wait_time:
        :param action:
        :param target:
        :return:
        """
        if not self.wait_until_appear(target, wait_time):
            return False
        click_x, click_y = target.coord()
        if action is None:
            self.device.click(click_x, click_y, control_name=target.name)
        elif isinstance(action, RuleLongClick):
            self.device.long_click(click_x, click_y, duration=action.duration / 1000, control_name=target.name)
        elif isinstance(action, RuleClick):
            self.device.click(click_x, click_y, control_name=target.name)
        return True

    def wait_until_disappear(self, target: RuleImage) -> None:
        while 1:
            self.screenshot()
            if not self.appear(target):
                break

    def wait_until_pos_stable(self, target: RuleImage, stable_time: float = 0.3, timeout: float = 2,
                              threshold: float = None, skip_first_screenshot: bool = True) -> bool:
        """
        等待直到在同一位置稳定出现
        :param skip_first_screenshot:
        :param threshold: target匹配阈值
        :param target: 目标图像
        :param stable_time: 判断是否稳定的时间
        :param timeout: 等待稳定的超时时间
        :return: timer时间内稳定出现则返回True, 否则False
        """
        logger.info(f'Wait until {target.name} position stable')
        timeout_timer = Timer(timeout).start()
        stable_timer = Timer(stable_time).start()
        pre_roi_front, cur_roi_front = None, None
        origin_roi_back = target.roi_back
        while not timeout_timer.reached():
            self.maybe_screenshot(skip_first_screenshot)
            skip_first_screenshot = False
            # 当前页面能够匹配到target
            if target.match(self.device.image, threshold=threshold):
                cur_roi_front = target.roi_front
                logger.info(f'Current:{cur_roi_front}, pre:{pre_roi_front}')
                target.roi_back = pre_roi_front
                # 上一次匹配到的位置还能匹配到target
                if pre_roi_front is not None and target.match(self.device.image, threshold=threshold):
                    # 到达稳定时间
                    if stable_timer.reached():
                        logger.info(f'{target.name} position has stabilized')
                        target.roi_back = origin_roi_back
                        return True
                else:
                    stable_timer.reset()  # 上一次匹配到的位置这次匹配不到了, 重置定时器
            else:
                stable_timer.reset()  # 当前页面都匹配不到, 重置定时器
            # 记录这一次的target位置
            pre_roi_front = cur_roi_front
            # 还原target的匹配区域
            target.roi_back = origin_roi_back
        logger.warning(f'Wait until pos stable({target}) timeout')
        return False

    def wait_until_stable(self,
                          target: RuleImage,
                          timer=Timer(0.3, count=1),
                          timeout=Timer(5, count=10),
                          skip_first_screenshot=True):
        """
        等待目标稳定，即连续多次匹配成功
        :param target:
        :param timer:
        :param timeout:
        :param skip_first_screenshot:
        :return:
        """
        target._match_init = False
        timeout.reset()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.screenshot()

            if target._match_init:
                if target.match(self.device.image):
                    if timer.reached():
                        break
                else:
                    # button.load_color(self.device.image)
                    timer.reset()
            else:
                # target.load_color(self.device.image)
                target._match_init = True

            if timeout.reached():
                logger.warning(f'Wait_until_stable({target}) timeout')
                break

    def wait_animate_stable(self, rule: RuleAnimate, interval: float = None, timeout: float = None):
        """
        不同与上面的wait_until_stable，这个将会匹配连续的两帧图片的特定区域
        @param rule:
        @param interval:
        @param timeout:
        @return:
        """
        if not isinstance(rule, RuleAnimate):
            rule = RuleAnimate(rule)
        timeout_timer = Timer(timeout).start() if timeout is not None else None
        while 1:
            self.screenshot()

            if interval:
                if rule.name in self.interval_timer:
                    if self.interval_timer[rule.name].limit != interval:
                        self.interval_timer[rule.name] = Timer(interval)
                else:
                    self.interval_timer[rule.name] = Timer(interval)
                if not self.interval_timer[rule.name].reached():
                    return False

            stable = rule.stable(self.device.image)
            if stable:
                if interval:
                    self.interval_timer[rule.name].reset()
                break

            if timeout_timer and timeout_timer.reached():
                logger.info(f'Wait_animate_stable({rule}) timeout')
                break

    def swipe(self, swipe: RuleSwipe, interval: float = None, duration: tuple | float = None) -> None:
        """

        :param interval:
        :param swipe:
        :param duration: 滑动时长（秒），float 或 (min, max)；None 用 device.swipe 默认 (0.1, 0.2) 随机
        :return:
        """
        if not isinstance(swipe, RuleSwipe):
            return

        if interval:
            if swipe.name in self.interval_timer:
                # 如果传入的限制时间不一样，则替换限制新的传入的时间
                if self.interval_timer[swipe.name].limit != interval:
                    self.interval_timer[swipe.name] = Timer(interval)
            else:
                # 如果没有限制时间，则创建限制时间
                self.interval_timer[swipe.name] = Timer(interval)
            # 如果时间还没到达，则不执行
            if not self.interval_timer[swipe.name].reached():
                return

        x1, y1, x2, y2 = swipe.coord()
        # 指定 duration 时透传给 device.swipe，走 control_method 分派与拟人化路径
        if duration is not None:
            self.device.swipe(p1=(x1, y1), p2=(x2, y2), control_name=swipe.name, duration=duration)
        else:
            self.device.swipe(p1=(x1, y1), p2=(x2, y2), control_name=swipe.name)

        # 执行后，如果有限制时间，则重置限制时间
        if interval:
            # logger.info(f'Swipe {swipe.name}')
            self.interval_timer[swipe.name].reset()

    def click(self, click: Union[RuleClick, RuleLongClick, RuleImage, RuleOcr] = None, interval: float = None) -> bool:
        """
        点击或者长按
        :param interval:
        :param click:
        :return: 返回值不是click是否成功，而是interval是否设置以及是否到时间
        """
        if not click:
            return False

        if interval:
            if click.name in self.interval_timer:
                # 如果传入的限制时间不一样，则替换限制新的传入的时间
                if self.interval_timer[click.name].limit != interval:
                    self.interval_timer[click.name] = Timer(interval)
            else:
                # 如果没有限制时间，则创建限制时间
                self.interval_timer[click.name] = Timer(interval)
            # 如果时间还没到达，则不执行
            if not self.interval_timer[click.name].reached():
                return False

        x, y = click.coord()
        if isinstance(click, RuleLongClick):
            self.device.long_click(x=x, y=y, duration=click.duration / 1000,
                                   control_name=click.name, control_roi=click.roi_front)
        elif isinstance(click, RuleClick):
            # RuleClick 的 roi_front 是意图层稳定区域（静态定义或任务显式改写），
            # 传给 humanize 参与同一资源判别；RuleImage/RuleOcr 的区域来自匹配
            # 结果（有抖动），不传、退化为按名判重
            self.device.click(x=x, y=y, control_name=click.name, control_roi=click.roi_front)
        elif isinstance(click, RuleImage) or isinstance(click, RuleOcr):
            self.device.click(x=x, y=y, control_name=click.name)

        # 执行后，如果有限制时间，则重置限制时间
        if interval:
            self.interval_timer[click.name].reset()
            return True
        return False

    def ocr_appear(self, target: RuleOcr, interval: float = None) -> bool:
        """
        ocr识别目标
        :param interval:
        :param target:
        :return: 如果target有keyword或者是keyword存在，返回是True，否则返回False
                 但是没有指定keyword，返回的是匹配到的值，具体取决于target的mode
        """
        if not isinstance(target, RuleOcr):
            return None
        #logger.info(f'Trying to OCR target.area{target.area} target.roi{target.roi} for {target.name}')
        if interval:
            if target.name in self.interval_timer:
                # 如果传入的限制时间不一样，则替换限制新的传入的时间
                if self.interval_timer[target.name].limit != interval:
                    self.interval_timer[target.name] = Timer(interval)
            else:
                # 如果没有限制时间，则创建限制时间
                self.interval_timer[target.name] = Timer(interval)
            # 如果时间还没到达，则不执行
            if not self.interval_timer[target.name].reached():
                return None
        
        result = target.ocr(self.device.image)
        appear = False

        if not target.keyword or target.keyword == '':
            appear = False
        match target.mode:
            case OcrMode.FULL:  # 全匹配
                appear = result != (0, 0, 0, 0)
            case OcrMode.SINGLE:
                appear = result == target.keyword
            case OcrMode.DIGIT:
                appear = result == int(target.keyword)
            case OcrMode.DIGITCOUNTER:
                appear = result == target.ocr_str_digit_counter(target.keyword)
            case OcrMode.DURATION:
                appear = result == target.parse_time(target.keyword)

        if interval and appear:
            self.interval_timer[target.name].reset()

        return appear

    def ocr_appear_click(self,
                         target: RuleOcr,
                         action: Union[RuleClick, RuleLongClick] = None,
                         interval: float = None,
                         duration: float = None) -> bool:
        """
        ocr识别目标，如果目标存在，则触发动作
        :param target:
        :param action:
        :param interval:
        :param duration:
        :return:
        """
        area = None
        if target.area == [0, 0, 100, 100]:
           area = target.area
           logger.info(f'Trying to OCR target.area{target.area} area{area}')
        appear = self.ocr_appear(target, interval)
        logger.info(f'Trying to OCR target.area{target.area} target.roi{target.roi} for {target.name}')
        if not appear:
            return False

        if action:
            x, y = action.coord()
            self.click(action, interval)
        else:
            x, y = target.coord()
            self.device.click(x=x, y=y, control_name=target.name)
        if area == [0, 0, 100, 100]:
            target.area=area
            logger.info(f'Trying to OCR target.area{target.area} area{area}')
        return True

    def list_find(self, target: RuleList, name: str | list[str], max_swipe: int = 15) -> bool | tuple:
        """
        会一致在列表寻找目标，找到了就退出。
        如果是图片列表会一直往下找
        如果是纯文字的，会自动识别自己的位置，根据位置选择向前还是向后翻
        :param max_swipe: 最大滑动次数
        :param target:
        :param name:
        :return:
        """
        swipe_down = False
        swipe_distance_ratio = None
        result = None
        if not target:
            return False
        appear = False
        for _ in range(max_swipe):
            self.screenshot()
            if target.is_image:
                result = target.image_appear(self.device.image, name=name)
                swipe_down = True
            elif target.is_ocr:
                result = target.ocr_appear(self.device.image, name=name)
                swipe_down = result is not None and isinstance(result, int) and result > 0
                swipe_distance_ratio = 1
            # 结果是坐标证明找到了, 非坐标都是没找到
            if result is not None and isinstance(result, tuple) and result!=(0, 0):
                appear = True
                break
            if swipe_distance_ratio:
                x1, y1, x2, y2 = target.swipe_pos(number=swipe_distance_ratio, after=swipe_down)
            else:
                x1, y1, x2, y2 = target.swipe_pos(after=swipe_down)
            self.device.swipe(p1=(x1, y1), p2=(x2, y2))
            self.device.click_record_clear()
            sleep(random.uniform(0.8, 1.3))  # 等待滑动完成, 待优化
        if appear:
            return result
        return False

    def list_appear_click(self, target: RuleList, interval: float = None, max_swipe: int = 10) -> bool:
        if interval:
            if target.name in self.interval_timer:
                # 如果传入的限制时间不一样，则替换限制新的传入的时间
                if self.interval_timer[target.name].limit != interval:
                    self.interval_timer[target.name] = Timer(interval)
            else:
                # 如果没有限制时间，则创建限制时间
                self.interval_timer[target.name] = Timer(interval)
            # 如果时间还没到达，则不执行
            if not self.interval_timer[target.name].reached():
                return False
        appear = self.list_find(target, name=target.array[0], max_swipe=max_swipe)
        if isinstance(appear, tuple) and interval:
            x, y = appear
            self.device.click(x, y)
            self.interval_timer[target.name].reset()
            return True
        return False

    def set_next_run(self, task: str, finish: bool = False,
                     success: bool = None, server: bool = True, target: datetime = None,
                     persist: bool = True) -> None:
        """
        设置下次运行时间  当然这个也是可以重写的
        :param persist: 是否立即保存 next_run；False 时由调用方统一保存其他配置修改
        :param target: 可以自定义的下次运行时间
        :param server: True
        :param success: 判断是成功的还是失败的时间间隔
        :param task: 任务名称，大驼峰的
        :param finish: 是完成任务后的时间为基准还是开始任务的时间为基准
        :return:
        """
        if finish:
            start_time = datetime.now().replace(microsecond=0)
        else:
            start_time = self.start_time
        self.config.task_delay(task, start_time=start_time, success=success,
                               server=server, target=target, persist=persist)

    def custom_next_run(self, task: str, custom_time: Time = None, time_delta: float = 1) -> None:
        """
        设置下次自定义运行时间
        :param task: 任务名称，大驼峰的
        :param custom_time: 可以自定义的下次运行时间
        :param time_delta: 下次运行日期为几天后，默认为第二天
        :return:
        """
        target_time = (datetime.now() + timedelta(days=time_delta)).replace(hour=custom_time.hour,
                                                                            minute=custom_time.minute,
                                                                            second=custom_time.second)
        self.set_next_run(task, target=target_time)

    def apply_random_delay(self, task: str, target: datetime) -> datetime:
        """
        给下次运行时间叠加随机延时（防检测），未启用时原样返回。
        适用于任务正常完成后的调度点（如挂卡/寄养后按卡剩余时间设定下次运行）。

        :param task: 任务名称，大驼峰的，如 'KekkaiUtilize'（需在 FORBIDDEN_TIME_TASKS 注册）
        :param target: 原定的下次运行时间
        :return: 叠加随机延时后的下次运行时间
        """
        random_delay = self.config.get_task_random_delay(task)
        if random_delay is None:
            return target
        target = target + random_delay
        logger.info(f'[{task}] 下次运行时间叠加随机延时 {random_delay}，调整为 {target}')
        return target

    def check_forbidden_time(self, task: str, enable: bool, time_range: str) -> None:
        """
        检查当前时间是否落在禁止运行时间段内。
        命中则把下次运行时间设为该区间结束时刻，并抛出 TaskEnd 跳过本次运行。
        跨天区间（如 23:00-01:00）命中后结束时间会落在次日，由 forbidden_range_end 处理。
        开启随机延时配置时，下次运行时间在区间结束时刻上再叠加随机分钟数，
        避免每次都在解禁的整点准点上线。

        :param task: 任务名称，大驼峰的，如 'KekkaiUtilize'
        :param enable: 是否启用禁止时间段功能
        :param time_range: 禁止时间段配置字符串，如 "01:00-02:00,02:30-04:00"
        :return:
        """
        if not enable:
            return
        end_dt = forbidden_range_end(datetime.now(), time_range)
        if end_dt is None:
            return
        # 禁止时间段解禁时刻叠加随机延时，避免每次都在解禁的整点准点上线
        random_delay = self.config.get_task_random_delay(task)
        if random_delay is not None:
            end_dt = end_dt + random_delay
            logger.info(f'[{task}] 禁止时间段解禁后叠加随机延时 {random_delay}')
        logger.info(f'[{task}] 当前处于禁止运行时间段内，跳过本次运行，下次运行时间设为 {end_dt}')
        self.set_next_run(task, target=end_dt, server=False)
        raise TaskEnd(task)

    def next_run_week(self, target_day: int = 1, push_notify: bool = True):
        """
        计算下一次运行的时间，目标是每周的特定一天。

        参数:
        target_day (int): 目标运行的日，取值1到7代表周一到周日，默认为1（周一）。
        """
        
        def convert_week_to_number(week_day: Week) -> int:
            """
            将 Week 枚举转换为对应的数字
            周一对应 1，周二对应 2，... 周日对应 7

            :param week_day: Week 枚举值
            :return: 对应的数字 (1-7)
            """
            week_map = {
                Week.mon: 1,
                Week.tue: 2,
                Week.wed: 3,
                Week.thu: 4,
                Week.fri: 5,
                Week.sat: 6,
                Week.sun: 7
            }

            return week_map.get(week_day, 0)  # 如果找不到返回0

        if isinstance(target_day, Week):
            target_day = convert_week_to_number(target_day)

        today = datetime.today()
        current_weekday = today.weekday()  # 周一为0，周日为6
        target = target_day - 1  # 将输入1-7转换为0-6
        days_diff = (target - current_weekday) % 7 or 7

        TaskName = self.config.task.command
        logger.info(f'{TaskName} done in {days_diff} days on next Week [{target_day}].')
        from module.config.utils import convert_to_underscore
        # 获取服务更新时间配置
        task_name = convert_to_underscore(TaskName)
        task_object = getattr(self.config.model, task_name, None)
        scheduler = getattr(task_object, 'scheduler', None)
        server_update = scheduler.server_update
        if push_notify:
            self.push_notify(content=f'任务下周{target_day}执行')

        # 调用自定义函数设置下一次运行时间
        self.custom_next_run(task=TaskName,
                             custom_time=Time(hour=server_update.hour, minute=server_update.minute,
                                              second=server_update.second),
                             time_delta=days_diff)
    #  ---------------------------------------------------------------------------------------------------------------
    #
    #  ---------------------------------------------------------------------------------------------------------------
    def ui_reward_appear_click(self, screenshot=False) -> bool:
        """
        如果出现 ‘获得奖励’ 就点击
        :return:
        """
        if screenshot:
            self.screenshot()
        return self.appear_then_click(self.I_UI_REWARD, action=self.C_UI_REWARD, interval=0.4, threshold=0.6)

    def ui_get_reward(self, click_image: RuleImage or RuleOcr or RuleClick, click_interval: float = 1):
        """
        传进来一个点击图片 或是 一个ocr， 会点击这个图片，然后等待‘获得奖励’，
        最后当获得奖励消失后 退出
        :param click_interval:
        :param click_image:
        :return:
        """
        _timer = Timer(10)
        _timer.start()
        while 1:
            self.screenshot()

            if self.ui_reward_appear_click():
                sleep(0.5)
                while 1:
                    self.screenshot()
                    # 等待动画结束
                    if not self.appear(self.I_UI_REWARD, threshold=0.6):
                        logger.info('Get reward success')
                        break

                    # 一直点击
                    if self.ui_reward_appear_click():
                        continue
                break
            if _timer.reached():
                logger.warning('Get reward timeout')
                break

            if isinstance(click_image, RuleImage):
                if self.appear_then_click(click_image, interval=click_interval):
                    continue
            elif isinstance(click_image, RuleOcr):
                if self.ocr_appear_click(click_image, interval=click_interval):
                    continue
            elif isinstance(click_image, RuleClick):
                if self.click(click_image, interval=click_interval):
                    continue

        return True

    def ui_click(self, click, stop, interval=1, timeout=None):
        """
        循环的一个操作，直到出现stop
        :param click:
        :param stop:
        :param interval: 点击间隔
        :param timeout: 超时时间（秒），None表示不超时
        :return:
        """
        timer = Timer(timeout).start() if timeout else None
        while 1:
            self.screenshot()
            if self.appear(stop):
                return True
            if timer and timer.reached():
                logger.warning(f'ui_click timeout after {timeout}s')
                return False
            if isinstance(click, RuleImage) and self.appear_then_click(click, interval=interval):
                continue
            if isinstance(click, RuleClick) and self.click(click, interval=interval):
                continue
            elif isinstance(click, RuleOcr) and self.ocr_appear_click(click, interval=interval):
                continue

    def ui_clicks(self, clicks: list[RuleImage | RuleOcr | RuleClick], stop: RuleImage, interval=1):
        while 1:
            self.screenshot()
            if self.appear(stop):
                break
            for click in clicks:
                if isinstance(click, RuleImage) and self.appear_then_click(click, interval=interval):
                    continue
                elif isinstance(click, RuleClick) and self.click(click, interval=interval):
                    continue
                elif isinstance(click, RuleOcr) and self.ocr_appear_click(click, interval=interval):
                    continue

    def ui_click_until_disappear(self, click, interval: float = 1):
        """
        点击一个按钮直到消失
        :param interval:
        :param click:
        :return:
        """
        while 1:
            self.screenshot()
            if not self.appear(click):
                break
            elif self.appear_then_click(click, interval=interval):
                continue

    def ui_click_until_smt_disappear(self, click, stop, interval: float = 1):
        """
        点击一个按钮/区域/文字直到stop消失

        """
        while 1:
            self.screenshot()
            if not self.appear(stop):
                break
            if isinstance(click, RuleImage) or isinstance(click, RuleGif):
                self.appear_then_click(click, interval=interval)
                continue
            if isinstance(click, RuleClick):
                self.click(click, interval)
                continue
            if isinstance(click, RuleOcr):
                self.click(click)
                continue

    def ui_click_multi_scale(self, click, stop, interval=1, scale_range=None, timeout=None):
        """
        循环的一个操作，直到出现stop（支持多尺度图片识别）
        :param click:
        :param stop:
        :param interval:
        :param scale_range: 多尺度缩放范围 (start, end, step)
        :param timeout: 超时时间（秒），None表示不超时
        :return: True-找到stop条件, False-超时
        """
        timer = Timer(timeout).start() if timeout else None
        while 1:
            self.screenshot()
            if self.appear(stop):
                return True
            if timer and timer.reached():
                logger.warning(f'ui_click_multi_scale timeout after {timeout}s')
                return False
            if isinstance(click, RuleImage) and self.appear_then_click_multi_scale(click, scale_range=scale_range, interval=interval):
                continue
            if isinstance(click, RuleClick) and self.click(click, interval=interval):
                continue
            elif isinstance(click, RuleOcr) and self.ocr_appear_click(click, interval=interval):
                continue

    def push_notify(self, content='', title=None, level=3):
        logger.info(f'Push notify: {content}')

    def save_image(self, task_name=None, content=None, wait_time=2, image_type=False, push_flag=False, level=3, custom_roi=None):
        """
        使用cv2保存截图
        :param task_name: 图片保存的文件名
        :param content: 日志内容
        :param wait_time: 等待时间后截图
        :param image_type: 是否为图片类型
        :param push_flag: 是否推送通知
        :param level: 日志等级
        :param custom_roi: 自定义感兴趣区域 (x, y, w, h)
        """
        import time
        time.sleep(wait_time)  # 等待指定时间
        
        # 获取当前截图
        image = self.screenshot()
        
        # 如果指定了ROI，则裁剪图像
        if custom_roi:
            x, y, w, h = custom_roi
            image = image[y:y+h, x:x+w]
        
        # 生成文件名
        if task_name is None:
            task_name = f"screenshot_{int(time.time())}"
        
        # 每个 task 的截图统一保存到 screenshots/<task名>_Screenshots/ 下
        # （task 名取当前调度任务的 command，直接实例化调试时退化为 Default）
        task_command = getattr(getattr(self.config, 'task', None), 'command', None) or 'Default'
        folder = f'./screenshots/{task_command}_Screenshots'
        if not os.path.exists(folder):
            os.makedirs(folder)
        
        # 添加时间戳到文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 包含毫秒的时间戳
        filename = f"{task_name}_{timestamp}.png"
        filepath = os.path.join(folder, filename)
        
        # 使用cv2保存图像
        success = cv2.imwrite(filepath, image)
        
        if success:
            logger.info(f"Save image: {filepath}")
            if content:
                logger.log(level, content)
            
            # 如果需要推送通知
            if push_flag:
                self.push_notify(content=content or f"Saved image: {filename}", level=level)
        else:
            logger.error(f"Failed to save image: {filepath}")

    def appear_rgb(self, target, image=None, difference: int = 10):
        """
        判断目标的平均颜色是否与图像中的颜色匹配。
        参数:
        - target: 目标对象，包含目标的文件路径和区域信息。
        - image: 输入图像，如果未提供，则使用设备捕获的图像。
        - difference: 颜色差异阈值，默认为10。
        返回:
        - 如果目标颜色与图像颜色匹配，则返回True，否则返回False。
        """
        # 如果未提供图像，则使用设备捕获的图像
        # logger.info(f"target [{target}], image [{image}]")
        if not self.appear(target):
            logger.warning(f"[{target.name}]未匹配到")
            return False

        if image is None:
            image = self.device.image

        # 加载图像并计算其平均颜色
        img = cv2.imread(target.file)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        average_color = cv2.mean(img_rgb)
        # logger.info(f"[{target.name}]average_color: {average_color}")

        # 提取目标区域的坐标和尺寸，并确保它们为整数
        x, y, w, h = target.roi_front
        x, y, w, h = int(x), int(y), int(w), int(h)
        # 从输入图像中提取目标区域
        img = image[y:y + h, x:x + w]
        # 计算目标区域的平均颜色
        color = cv2.mean(img)
        # logger.info(f"[{target.name}] color: {color}")

        # 比较目标图像和目标区域的颜色差异
        for i in range(3):
            if abs(average_color[i] - color[i]) > difference:
                #logger.warning(f" [{target.name}] 颜色匹配失败")
                return False

        logger.info(f"[{target.name}] 颜色匹配成功")
        return True
    def input_text(self, text: str):
        """
        在设备上输入文本（包括字符和数字）
        注意：这要求设备上有一个可以接收输入的文本框处于焦点状态
        
        Args:
            text (str): 要输入的文本
        """
        # 使用设备的输入方法直接输入
        return self.rule_input.input_text(text)

    def input_text_alternative(self, text: str):
        """
        使用替代方法输入文本（逐字符输入）
        对于某些特殊字符或语言，这种方法可能更有效
        
        Args:
            text (str): 蟊要输入的文本
        """
        return self.rule_input.input_text_alternative(text)

    def input_number(self, number: Union[int, float, str]):
        """
        输入数字
        
        Args:
            number (int/float/str): 要输入的数字
        """
        return self.rule_input.input_number(number)
