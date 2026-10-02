# This Python file uses the following encoding: utf-8
# @brief    Ryou Dokan Toppa (阴阳竂道馆突破功能)
# @author   jackyhwei
# @note     draft version without full test
# github    https://github.com/roarhill/oas
import time
import os
import json
from time import sleep

import cv2
import re
import requests
import tasks.Dokan.inner_page as ipages
from datetime import datetime, timedelta
from enum import Enum
from module.atom.click import RuleClick
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger
from pathlib import Path
from tasks.Component.GeneralBattle.config_general_battle import GreenMarkType, GeneralBattleConfig
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralInvite.assets import GeneralInviteAssets
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Dokan.assets import DokanAssets
from tasks.Dokan.config import Dokan
from tasks.Dokan.inner_page import page_dokan
from tasks.GameUi.page import page_guild
from tasks.GameUi.game_ui import GameUi
from tasks.RichMan.assets import RichManAssets
from tasks.GameUi.page import PageRegistry
from module.atom.swipe import RuleSwipe



class DokanScene(Enum):
    # 未知界面
    RYOU_DOKAN_SCENE_UNKNOWN = 0
    # 进入道馆，集结中
    RYOU_DOKAN_SCENE_GATHERING = 1
    # 进入战场，等待用户点击开始战斗
    RYOU_DOKAN_SCENE_IN_FIELD = 2
    # 通常是失败了，并退出来到集结界面，可重新开始点击右下角挑战进入战斗
    RYOU_DOKAN_SCENE_START_CHALLENGE = 3
    # 失败次数超过上限，CD中
    RYOU_DOKAN_SCENE_CD = 4
    # 战斗进行中
    RYOU_DOKAN_SCENE_FIGHTING = 5
    # 加油进行中
    RYOU_DOKAN_SCENE_CHEERING = 6
    # 再战道馆投票
    RYOU_DOKAN_SCENE_FAILED_VOTE_NO = 7
    # 阴阳竂
    RYOU_DOKAN_RYOU = 8
    # 战斗结算，可能是打完小朋友了，也可能是失败了。
    RYOU_DOKAN_SCENE_BATTLE_OVER = 9
    # 等待BOSS战
    RYOU_DOKAN_SCENE_BOSS_WAITING = 10
    # 馆主战中,处于寮境
    RYOU_DOKAN_SCENE_MASTER_BATTLING = 11
    # 馆主战第一阵容
    RYOU_DOKAN_SCENE_BATTLE_MASTER_FIRST = 12
    # 馆主战第二阵容
    RYOU_DOKAN_SCENE_BATTLE_MASTER_SECOND = 13
    # 正在查找道馆,处于地图界面
    RYOU_DOKAN_SCENE_FINDING_DOKAN = 97
    
    

    def __str__(self):
        return self.name.title()


class ScriptTask(GeneralBattle,GameUi, SwitchSoul, DokanAssets, RichManAssets):
    """ 道馆 """
    team_switched: bool = False
    # 战斗次数
    battle_count: int = 0
    # 寮友进入道馆次数
    goto_dokan_num: int = 0
    # 今日是否第一次道馆 是否放弃
    dokan_quit: bool = False
    # 上一个场景
    last_scene = None
    # 查找的所有道馆
    find_dokan_list = []
    # 开启的是否为福利寮
    open_welfare = False
    # 福利寮名单
    welfare_names = None
    # 福利寮创建时间
    create_doukan_time = None
    # 记录开启道馆时间,两次道馆要间隔15分钟
    first_open_dokan_time = None
    # 道馆可战斗次数
    dokan_battle_number = 0

    first_master_killed = False

    boss_battles = False

    # QQ群触发相关: 已处理过的消息ID集合，避免重复触发
    _qq_processed_ids: set = set()
    # 记录已处理过的最大message_seq，用于增量拉取时跳过旧消息
    _qq_last_message_seq: int = 0

    def welfare_name_str(self):
        """
        从配置文件加载福利寮名单，只加载一次
        """
        # 获取当前脚本所在目录，然后拼接文件名
        welfare_file = Path(__file__).parent / '福利寮名单.txt'
        try:
            with open(welfare_file, 'r', encoding='utf-8') as file:
                # 读取所有行并放到一个列表中
                lines = file.readlines()
                # 去除每行末尾的换行符并过滤空行
                content = [line.strip() for line in lines if line.strip()]
                return content
        except FileNotFoundError:
            self.push_notify(content=f"福利寮名单文件未找到: {welfare_file}")
            logger.warning(f"福利寮名单文件未找到: {welfare_file}")
            return ''
        except Exception as e:
            self.push_notify(content=f"读取福利寮名单时出错: {e}")
            logger.error(f"读取福利寮名单时出错: {e}")
            return ''

    def check_current_weekday(self, success=False):
        cfg: Dokan = self.config.dokan
        today = datetime.today()
        current_weekday = today.weekday()

        # 周一到周四(0-3): 放行星期几检查，后续仍需通过QQ群触发检测
        if current_weekday <= 3:
            return

        # 周五到周日(4-6): 根据周末配置决定
        if not cfg.dokan_config.enable_weekend_run:
            days_until_monday = (7 - current_weekday) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            self.set_next_run(task='Dokan', target=datetime.now() + timedelta(days=days_until_monday))
            self.finish_task()
            return

        run_today = False
        if current_weekday == 4 and cfg.dokan_config.run_on_friday:
            run_today = True
        elif current_weekday == 5 and cfg.dokan_config.run_on_saturday:
            run_today = True
        elif current_weekday == 6 and cfg.dokan_config.run_on_sunday:
            run_today = True

        if run_today:
            # 放行，后续仍需通过QQ群触发检测
            return
        else:
            self.set_next_run(task='Dokan', target=datetime.now() + timedelta(days=1))
            self.finish_task()

    def check_qq_group_message(self, cfg_trigger) -> bool:
        """
        检查QQ群是否满足道馆触发条件:
        1. 特定成员发送包含"道馆已经创建"的消息
        2. 独立的一条@全体成员消息
        两条消息都需要在有效时间窗口内才算触发

        通过OneBot HTTP API (go-cqhttp/NapCat/Lagrange) 获取群消息历史

        :param cfg_trigger: QQGroupTriggerConfig 配置对象
        :return: 是否检测到触发消息
        """
        try:
            # 构建请求
            endpoint = cfg_trigger.endpoint
            if '//' not in endpoint:
                endpoint = f'http://{endpoint}'
            url = f'{endpoint}/get_group_msg_history'

            headers = {'Content-Type': 'application/json'}
            if cfg_trigger.access_token:
                headers['Authorization'] = f'Bearer {cfg_trigger.access_token}'

            # 只检查今天21:00之后的消息
            now = datetime.now()
            today_9pm = datetime.combine(now.date(), datetime.min.time().replace(hour=21))
            # 如果当前还没到21点，说明还没到监控时间，直接返回
            if now < today_9pm:
                logger.info("当前未到21:00，跳过QQ群消息检查")
                return False
            nine_pm_timestamp = today_9pm.timestamp()

            # 获取消息
            # 策略: 逐步增大拉取数量(100→200→300...), 直到获取到阈值时间之前的消息为止
            #   message_seq=0 → 返回最新消息
            #   count=N → 返回最近N条消息(包含更早的历史)
            #   如果拉取的消息中最晚早于阈值时间,说明已经覆盖到目标时间段
            all_messages = []
            is_first_fetch = len(self._qq_processed_ids) == 0
            fetch_count = 50
            max_count = 1000  # 单次最大拉取数量上限
            last_msg_count = 0  # 上一次拉取到的消息数，用于判断是否还有更多历史

            for attempt in range(10):  # 最多尝试10次递增
                body = {
                    'group_id': cfg_trigger.group_id,
                    'message_seq': 0,
                    'count': fetch_count
                }
                resp = requests.post(url, json=body, headers=headers, timeout=10)
                if resp.status_code != 200:
                    logger.warning(f"QQ群消息请求失败，状态码: {resp.status_code}")
                    break
                data = resp.json()
                if data.get('status') != 'ok':
                    logger.warning(f"QQ群消息API返回错误: {data.get('wording', data)}")
                    break
                all_messages = data.get('data', {}).get('messages', [])

                # 按 message_id 去重
                seen_ids = set()
                unique_messages = []
                for msg in all_messages:
                    mid = msg.get('message_id')
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        unique_messages.append(msg)
                all_messages = unique_messages
                current_count = len(all_messages)

                # 检查是否已获取到阈值时间之前的消息(即最早有效消息的time < nine_pm_timestamp)
                valid_times = [msg.get('time', 0) for msg in all_messages if msg.get('time', 0) > 0]
                if not valid_times:
                    break
                earliest_time = min(valid_times)
                if earliest_time < nine_pm_timestamp:
                    # 已获取到阈值时间之前的消息，无需继续拉取
                    logger.info(f"第{attempt + 1}次拉取count={fetch_count}条, "
                                f"最早消息时间={datetime.fromtimestamp(earliest_time).strftime('%H:%M:%S')}, "
                                f"已覆盖到阈值时间之前")
                    break

                # 如果增大count后消息数没有增加，说明已无更多历史
                if attempt > 0 and current_count <= last_msg_count:
                    logger.info(f"第{attempt + 1}次拉取count={fetch_count}条, "
                                f"实际返回{current_count}条(与上次{last_msg_count}条相同), 已无更多历史消息")
                    break

                # 还没到阈值时间，增大拉取数量
                logger.info(f"第{attempt + 1}次拉取count={fetch_count}条, "
                            f"实际返回{current_count}条, "
                            f"最早消息时间={datetime.fromtimestamp(earliest_time).strftime('%H:%M:%S')}, "
                            f"尚未覆盖到阈值时间, 增大拉取数量至{fetch_count + 50}")
                last_msg_count = current_count
                fetch_count += 50
                if fetch_count > max_count:
                    logger.warning(f"拉取数量已达上限{max_count}, 停止递增")
                    break

            # 按时间过滤: 只保留阈值时间之后的消息
            filtered_messages = []
            for msg in all_messages:
                msg_time = msg.get('time', 0)
                if msg_time > 0 and msg_time >= nine_pm_timestamp:
                    filtered_messages.append(msg)

            logger.info(f"获取到QQ群原始消息{len(all_messages)}条(count={fetch_count}), "
                        f"时间范围内{len(filtered_messages)}条"
                        f"({'首次全量获取' if is_first_fetch else '增量获取'})")

            all_messages = filtered_messages

            if not all_messages:
                logger.info("QQ群暂无时间范围内消息")
                return False

            create_keyword_found = False
            at_all_found = False

            for msg in all_messages:
                msg_id = msg.get('message_id', 0)
                sender_id = msg.get('sender', {}).get('user_id', 0)

                # 跳过已处理过的消息
                if msg_id in self._qq_processed_ids:
                    continue

                # 处理消息内容 (支持数组格式和CQ码字符串格式)
                message_content = msg.get('message', '')
                has_at_all = False
                has_image = False
                text_content = ''

                if isinstance(message_content, list):
                    # 数组格式 (OneBot v11 标准格式)
                    for seg in message_content:
                        if isinstance(seg, dict):
                            if seg.get('type') == 'at' and seg.get('data', {}).get('qq') == 'all':
                                has_at_all = True
                            elif seg.get('type') == 'text':
                                text_content += seg.get('data', {}).get('text', '')
                            elif seg.get('type') == 'image':
                                has_image = True
                elif isinstance(message_content, str):
                    # CQ码字符串格式
                    if '[CQ:at,qq=all]' in message_content:
                        has_at_all = True
                    if '[CQ:image' in message_content:
                        has_image = True
                    # 提取纯文本内容(去除CQ码)
                    text_content = re.sub(r'\[CQ:[^\]]+\]', '', message_content)

                # 标记消息已处理
                self._qq_processed_ids.add(msg_id)

                # 检查道馆创建关键词(由特定成员发送): 文字关键词 or 图片消息
                if cfg_trigger.create_keyword and cfg_trigger.create_keyword in text_content:
                    # 如果配置了指定发送者，则验证发送者
                    if cfg_trigger.create_sender_id == 0 or sender_id == cfg_trigger.create_sender_id:
                        create_keyword_found = True
                        logger.info(f"检测到道馆创建关键词消息，发送者QQ: {sender_id}")
                elif has_image:
                    # 图片消息也视为道馆创建信号
                    if cfg_trigger.create_sender_id == 0 or sender_id == cfg_trigger.create_sender_id:
                        create_keyword_found = True
                        logger.info(f"检测到道馆创建图片消息，发送者QQ: {sender_id}")

                # 检查@全体成员消息(独立消息，需验证发送者)
                if has_at_all:
                    # at_all_sender_id为0时与create_sender_id一致
                    at_sender = cfg_trigger.at_all_sender_id if cfg_trigger.at_all_sender_id != 0 else cfg_trigger.create_sender_id
                    if at_sender == 0 or sender_id == at_sender:
                        at_all_found = True
                        logger.info(f"检测到@全体成员消息，发送者QQ: {sender_id}")

            # 将所有消息ID标记为已处理
            for msg in all_messages:
                self._qq_processed_ids.add(msg.get('message_id', 0))

            # 判断是否满足触发条件
            if cfg_trigger.require_at_all:
                if create_keyword_found and at_all_found:
                    logger.info("QQ群触发条件满足: 道馆创建关键词 + @全体成员")
                    return True
                else:
                    if not create_keyword_found:
                        logger.info("未检测到道馆创建关键词消息")
                    if not at_all_found:
                        logger.info("未检测到@全体成员消息")
                    return False
            else:
                # 不要求@全体成员，只检测道馆创建关键词
                if create_keyword_found:
                    logger.info("QQ群触发条件满足: 道馆创建关键词")
                    return True
                else:
                    logger.info("未检测到道馆创建关键词消息")
                    return False

        except requests.exceptions.ConnectionError:
            logger.warning("无法连接到OneBot API，请检查NapCat是否运行")
            return False
        except requests.exceptions.Timeout:
            logger.warning("OneBot API请求超时")
            return False
        except Exception as e:
            logger.error(f"检查QQ群消息时出错: {e}")
            return False

    def _init_qq_last_message_id(self, cfg_trigger):
        """
        初始化QQ群最新消息ID，避免误触发历史消息
        """
        try:
            endpoint = cfg_trigger.endpoint
            if '//' not in endpoint:
                endpoint = f'http://{endpoint}'
            url = f'{endpoint}/get_group_msg_history'
            headers = {'Content-Type': 'application/json'}
            if cfg_trigger.access_token:
                headers['Authorization'] = f'Bearer {cfg_trigger.access_token}'
            body = {'group_id': cfg_trigger.group_id, 'message_seq': 0, 'count': 1}
            resp = requests.post(url, json=body, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                messages = data.get('data', {}).get('messages', [])
                if messages:
                    self._qq_last_message_id = max(msg.get('message_id', 0) for msg in messages)
                    logger.info(f"已记录当前QQ群最新消息ID: {self._qq_last_message_id}")
        except Exception as e:
            logger.warning(f"初始化QQ群消息ID失败: {e}")
    def get_requests(self, url):
        try:
            # 发送GET请求
            response = requests.get(url)
            logger.info(f"响应内容: {response.text}")
            # 检查请求是否成功
            if response.status_code == 200:
                return response
            else:
                logger.info(f"请求失败，状态码: {response.status_code}")
                self.push_notify(title="请求失败", content=f"状态码: {response.status_code}")
                return ""
        except requests.exceptions.RequestException as e:
            logger.error(f"请求发生错误: {e}")
            self.push_notify(title="请求发生错误", content=f"{e}")
            return ""
    def swipe(self, swipe: RuleSwipe, interval: float = None, duration: float = None) -> None:
        """

        :param interval:
        :param swipe:
        :param  duration: 手势总时长（秒）。None（默认）不传给 device.swipe，
            由 Control.swipe 按滑动距离推导时长；显式传入时以
            (duration, duration+0.1) 区间抽样
        :param  wait_up_time
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
        # duration 未指定时透传 None：Control.swipe 按滑动距离推导时长
        if duration is not None:
            self.device.swipe(p1=(x1, y1), p2=(x2, y2), control_name=swipe.name, duration=(duration, duration + 0.1))
        else:
            self.device.swipe(p1=(x1, y1), p2=(x2, y2), control_name=swipe.name)

        # 执行后，如果有限制时间，则重置限制时间
        if interval:
            # logger.info(f'Swipe {swipe.name}')
            self.interval_timer[swipe.name].reset()
    def run(self):
        # 检查今天周几
        self.check_current_weekday()

        cfg: Dokan = self.config.dokan

        # QQ群触发模式: 单次检查QQ群消息，没有触发消息则5分钟后重试
        if cfg.qq_group_trigger.enable:
            # _qq_last_message_id 为0时首次全量获取，非0时增量获取
            # 不再预初始化，首次会全量获取21:00之后的所有消息
            if not self.check_qq_group_message(cfg.qq_group_trigger):
                # 未检测到触发消息，按配置间隔重试
                retry_minutes = cfg.qq_group_trigger.retry_interval
                next_run = datetime.now() + timedelta(minutes=retry_minutes)
                logger.info(f"未检测到QQ群触发消息，{retry_minutes}分钟后重试，下次运行: {next_run.strftime('%H:%M:%S')}")
                self.config.notifier.push(title='道馆', content=f'未检测到QQ群触发消息，{retry_minutes}分钟后重试')
                self.set_next_run(task='Dokan', target=next_run)
                self.finish_task()
            else:
                logger.info("检测到QQ群触发消息，开始执行道馆任务")
                self.push_notify(content="QQ群触发道馆，开始执行道馆任务")
                self.config.notifier.push(title='道馆', content='QQ群触发道馆，开始执行道馆任务')

        # 发送请求检查福利寮开启情况
        if cfg.welfare_config.enable_get_requests:
            response = self.get_requests(cfg.welfare_config.get_requests_url)
            json_response = response.json()
            datetime_now = datetime.now()
            # 解析时间戳并设置创建道馆时间
            timestamp = json_response.get('timestamp')
            if not timestamp:
                self.set_next_run(task='Dokan', target=datetime_now + timedelta(minutes=3))
                self.finish_task()
            # 解析时间戳获取时分秒
            timestamp_time = datetime.fromtimestamp(timestamp)
            logger.info(f"福利道馆创建时间: {timestamp_time}")
            # 检查响应有效性
            if not json_response or not json_response.get('est', False):
                logger.warning(f"福利道馆未开启: {json_response}")
                if datetime_now.time() > timestamp_time.time():
                    self.set_next_run(task='Dokan', target=datetime_now + timedelta(minutes=3))
                    self.finish_task()
                self.set_next_run(task='Dokan', target=datetime.combine(datetime_now.date(), timestamp_time.time()))
                self.finish_task()

            # 获取明天的日期，但使用timestamp的时分秒
            tomorrow_date = (datetime_now + timedelta(days=1)).date()
            self.create_doukan_time = datetime.combine(tomorrow_date, timestamp_time.time())
            logger.info(f"明天道馆执行时间: {self.create_doukan_time}")

        # 加载福利寮名单
        self.welfare_names = self.welfare_name_str()

        # 开始道馆流程
        self.goto_dokan()
        self.dokan_process(cfg)

    def dokan_process(self, cfg: Dokan):
        # 开始道馆流程
        logger.info("开始道馆流程")
        scene_timer = Timer(50)
        scene_timer.start()
        timer_count = 1
        
        while 1:

            if scene_timer and scene_timer.reached():
                scene_timer.reset()
                if timer_count >= 100:
                    self.save_image(image_type=True, push_flag=True, content=f"道馆流程超时")
                    break
                timer_count += 1
                self.device.stuck_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')

            # 检测当前界面的场景
            in_dokan, current_scene = self.get_current_scene()

            # 如果当前不在道馆，或者被人工操作退出道馆了，重新尝试进入道馆
            if not in_dokan:
                try:
                    # 计算道馆战斗时间
                    if self.first_open_dokan_time is not None and self.dokan_battle_number == 2:
                        MIN_DOKAN_TIME = timedelta(minutes=15)
                        dakan_time = datetime.now() - self.first_open_dokan_time
                        logger.warning(f"道馆持续时间: {dakan_time}")
                        if dakan_time < MIN_DOKAN_TIME:
                            next_run_time = self.first_open_dokan_time + MIN_DOKAN_TIME
                            logger.warning(f"道馆战斗时间不足15分钟，设置下次运行时间: {next_run_time}")
                            self.set_next_run(task='Dokan', target=next_run_time)
                            self.finish_task()
                        else:
                            logger.warning(f"道馆持续时间超过15分钟,直接进行下一次道馆")
                except TaskEnd:
                    # 重新抛出TaskEnd异常，这是正常的流程控制
                    self.finish_task()
                except Exception as e:
                    logger.error(f"道馆流程异常: {e}", exc_info=True)
                    self.save_image(image_type=True, push_flag=True, content=f"道馆流程异常: {e}")

                # 重置换阵容和是否为福利寮
                self.team_switched = False
                self.open_welfare = False

                # 重新尝试进入道馆
                self.goto_dokan()
                continue

            # 场景状态：道馆集结中
            if current_scene == DokanScene.RYOU_DOKAN_SCENE_GATHERING:
                self.goto_dokan_num = 0
                self.first_master_killed = False

            # 场景状态：等待馆主战开始
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_BOSS_WAITING:
                self.boss_battles = True
                pass
            # 场景状态：右下角挑战
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_START_CHALLENGE:
                self.appear_then_click(self.I_RYOU_DOKAN_START_CHALLENGE, interval=1)
                time.sleep(1)
            # # 场景状态：进入馆员战斗，待准备
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_IN_FIELD:
                # 战斗
                self.dokan_battle(cfg)
            # 投票
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_FAILED_VOTE_NO:
                if self.appear_then_click(self.I_QUIT_DOKAN_SURE, interval=1):
                    pass
                if self.appear_then_click(self.I_CONTINUE_DOKAN, interval=1):
                    logger.info("点击, 再战道馆")
                    time.sleep(2)
                    continue
            # 场景状态：在寮境,馆主战进行中,且右下角有挑战
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_MASTER_BATTLING:
                count = 1
                logger.info(f"{current_scene} dokan_master_count:{count},first_master_killed:{self.first_master_killed}")
                if (count - (1 if self.first_master_killed else 0)) > 0:
                    logger.info("start Master_first")
                    self.click(self.I_RYOU_DOKAN_START_CHALLENGE, interval=2)
                    continue
                # 放弃突破
                if self.first_master_killed and self.dokan_quit and self.config.dokan.dokan_config.dokan_enable:
                    # logger.info("今日第一次道馆，放弃本次道馆")
                    time.sleep(5)
                    while 1:
                        self.screenshot()
                        if self.appear(self.I_CONTINUE_DOKAN, interval=1):
                            break
                        if self.appear(self.I_QUIT_DOKAN_OVER, interval=1):
                            time.sleep(5)
                            break
                        if self.appear_then_click(self.I_QUIT_DOKAN_SURE, interval=1):
                            continue
                        if self.appear_then_click(self.I_QUIT_DOKAN, interval=1):
                            continue
                    self.config.notifier.push(content=f'已放弃本次道馆', title='道馆')
                # 非寮管理，检测到放弃突破，点击同意
                if self.appear_then_click(self.I_CROWD_QUIT_DOKAN, interval=1):
                    logger.info("同意, 放弃本次道馆")
                    continue
            # 场景状态：馆主第一阵容 且战斗未开始
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_BATTLE_MASTER_FIRST:
                battle_success = self.dokan_battle(cfg, is_master_battle=True)
                if  battle_success:
                    # TEST 只打一次 应该是还在战斗界面 处于未准备界面(RYOU_DOKAN_SCENE_BATTLE_MASTER_SECOND)
                    self.first_master_killed = True
                    continue
                # 战斗失败的话 需要点击几次,退出战场界面(NOTE 此为猜测,具体情况不确定)
                # count = 0 时,正常不应进入此分支,可直接退出
                # count =2 时,dokan_battle 中 会打第一 第二 阵容,
                #          成功打掉第二阵容,应该有奖励图片,可 尝试退出
                #          攻打过程中失败,可尝试退出
                #          攻打过程中,其他人打完了,可尝试退出
                # 直接尝试退出
                self.first_master_killed = True
                self.quit_battle()
                continue
            # 场景状态：馆主第二阵容,且 战斗未开始
            elif current_scene == DokanScene.RYOU_DOKAN_SCENE_BATTLE_MASTER_SECOND:
                logger.warning("Second Master Battle")
                self.first_master_killed = True
                # 退出战斗界面
                self.quit_battle()
                continue

    def get_current_scene(self):
        """ 检测当前场景 """
        self.screenshot()
        self.device.click_record_clear()
        # 状态：是否完成馆员战斗
        if self.boss_battles == False and self.appear(self.I_RYOU_DOKAN_MASTER_BATTLE):
            self.boss_battles = True
            # 在等待馆主战时进行馆主阵容御魂切换
            self.dokan_switch_soul(mode='master')
        # 再战道馆
        if self.appear(self.I_CONTINUE_DOKAN):
            current_scene = DokanScene.RYOU_DOKAN_SCENE_FAILED_VOTE_NO
            if current_scene != self.last_scene:
                logger.info(f"再战道馆，投票场景")
                self.last_scene = current_scene
            return True, DokanScene.RYOU_DOKAN_SCENE_FAILED_VOTE_NO
        # 场景检测：阴阳竂
        if self.appear(self.I_SCENE_RYOU, threshold=0.8):
            logger.info(f"在阴阳寮中")
            return False, DokanScene.RYOU_DOKAN_RYOU
        # 场景检测：在庭院中
        if self.appear(self.I_BUFF_1, threshold=0.8):
            logger.info(f"在庭院中")
            return False, DokanScene.RYOU_DOKAN_SCENE_UNKNOWN
        # 场景检测：选寮界面
        if self.appear(self.I_FANGSHOU, threshold=0.8):
            logger.info(f"在选寮界面中")
            return False, DokanScene.RYOU_DOKAN_SCENE_FINDING_DOKAN
        # 状态：判断是否集结中
        if self.appear(self.I_RYOU_DOKAN_GATHERING, threshold=0.95):
            current_scene = DokanScene.RYOU_DOKAN_SCENE_GATHERING
            if current_scene != self.last_scene:
                logger.info(f"道馆集结中")
                self.last_scene = current_scene
            return True, DokanScene.RYOU_DOKAN_SCENE_GATHERING
        # 状态：等待馆主战开始    
        if self.appear(self.I_DOKAN_BOSS_WAITING):
            current_scene = DokanScene.RYOU_DOKAN_SCENE_BOSS_WAITING
            if current_scene != self.last_scene:
                logger.info(f"等待馆主战中")
                self.last_scene = current_scene
            return True, DokanScene.RYOU_DOKAN_SCENE_BOSS_WAITING
        # 状态: 馆主战开始,且可以挑战
        if self.boss_battles == True and self.appear(self.I_RYOU_DOKAN_START_CHALLENGE):
            return True, DokanScene.RYOU_DOKAN_SCENE_MASTER_BATTLING
        # 馆主战斗中,未开始战斗状态
        if self.appear(self.I_RYOU_DOKAN_BATTLE_MASTER_FIRST) and \
                self.appear(self.I_PREPARE_HIGHLIGHT):
            return True, DokanScene.RYOU_DOKAN_SCENE_BATTLE_MASTER_FIRST
        if self.appear(self.I_RYOU_DOKAN_BATTLE_MASTER_SECOND) and \
                self.appear(self.I_PREPARE_HIGHLIGHT):
            return True, DokanScene.RYOU_DOKAN_SCENE_BATTLE_MASTER_SECOND
        
        
        # 状态：检查右下角有没有挑战？通常是失败了，并退出来到集结界面，可重新开始点击右下角挑战进入战斗
        if self.boss_battles == False and self.appear(self.I_RYOU_DOKAN_START_CHALLENGE, 0.95):
            if self.appear_rgb(self.I_RYOU_DOKAN_START_CHALLENGE):
                logger.info(f"挑战次数已重置")
                time.sleep(1)
                return True, DokanScene.RYOU_DOKAN_SCENE_START_CHALLENGE
            else:
                current_scene = DokanScene.RYOU_DOKAN_SCENE_GATHERING
                if current_scene != self.last_scene:
                    logger.info(f"道馆集结中,挑战未就绪")
                    self.last_scene = current_scene
                return True, DokanScene.RYOU_DOKAN_SCENE_GATHERING

        # 状态：进入战斗，待开始
        if self.boss_battles == False and self.appear(self.I_RYOU_DOKAN_IN_FIELD, threshold=0.85):
            logger.info(f"开始点击准备中")
            return True, DokanScene.RYOU_DOKAN_SCENE_IN_FIELD
        # 状态：战斗结算，可能是打完小朋友了，也可能是失败了。
        if self.appear(self.I_RYOU_DOKAN_BATTLE_OVER, threshold=0.85):
            logger.info(f"打完看到魂奖励中")
            self.dokan_save_image()
            self.appear_then_click(self.I_RYOU_DOKAN_BATTLE_OVER)
            return True, DokanScene.RYOU_DOKAN_SCENE_BATTLE_OVER
        # 如果出现失败 就点击
        if self.appear(GeneralBattle.I_FALSE, threshold=0.8):
            self.appear_then_click(GeneralBattle.I_FALSE)
            logger.info("战斗失败，返回")
            return True, DokanScene.RYOU_DOKAN_SCENE_BATTLE_OVER
        # 如果出现成功 就点击
        if self.appear(GeneralBattle.I_WIN, threshold=0.8):
            self.appear_then_click(GeneralBattle.I_WIN)
            logger.info("战斗成功，鼓，返回")
            return True, DokanScene.RYOU_DOKAN_SCENE_BATTLE_OVER
        # 状态：达到失败次数，CD中
        if self.appear(self.I_RYOU_DOKAN_CD, threshold=0.8):
            current_scene = DokanScene.RYOU_DOKAN_SCENE_CD
            if current_scene != self.last_scene:
                logger.info(f"等待挑战次数，观战中")
                self.last_scene = current_scene
            return True, DokanScene.RYOU_DOKAN_SCENE_CD
        
        

        return True, DokanScene.RYOU_DOKAN_SCENE_UNKNOWN
    def dokan_save_image(self):
        sleep(1)
        self.screenshot()
        from module.base.utils import save_image
        now=datetime.now()
        # 道馆截图统一保存到 screenshots/Dokan_Screenshots/ 下
        folder_name = f'screenshots/Dokan_Screenshots'
        if not os.path.exists(f'./{folder_name}'):
            os.makedirs(f'./{folder_name}')
        folder = f'./{folder_name}'
        save_image(self.screenshot(), f'{folder}/道馆_{now.year}-{now.month:02d}-{now.day:02d}     {now.hour:02d}-{now.minute:02d}-{now.second:02d}.png')
        
        
    def dokan_battle(self, cfg: Dokan, is_master_battle=False):
        """ 道馆战斗
        道馆集结结束后会自动进入战斗，打完一个也会自动进入下一个，因此直接点击右下角的开始
        :return: 战斗成功(True) or 战斗失败(False) or 区域不可用（False）
        """
        # 根据是否为馆主战选择对应的配置
        if is_master_battle:
            config: GeneralBattleConfig = cfg.general_battle_config_master
        elif self.open_welfare == False:
            config: GeneralBattleConfig = cfg.general_battle_config
        else:
            config: GeneralBattleConfig = cfg.general_battle_config2
            
        # 统一处理队伍切换逻辑，无论是普通战斗还是馆主战
        logger.info(f"switch team preset: enable={config.preset_enable}, preset_group={config.preset_group}, preset_team={config.preset_team}")
        self.switch_preset_team(config.preset_enable, config.preset_group, config.preset_team)
        self.team_switched = True
        # 切完队伍后有时候会卡顿，先睡一觉，防止快速跳到绿标流程，导致未能成功绿标
        time.sleep(0.5)


        while 1:
            self.screenshot()

            # 打完一个小朋友，自动进入下一个小朋友
            if self.appear(self.I_RYOU_DOKAN_IN_FIELD):
                self.battle_count += 1
                logger.info(f"第 {self.battle_count} 次战斗")
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_IN_FIELD)
                # 绿标
                self.dokan_green_mark(config.green_enable, config.green_mark)
                self.device.click_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')
                if self.boss_battles == True:
                    return True

            # 战斗时间已到，无奖励，在等待馆主战场景
            if self.appear(self.I_DOKAN_BOSS_WAITING, threshold=0.8):
                logger.info(f"战斗时间已到，无奖励，等待馆主战中")
                break

            # 如果出现赢 就点击
            if self.appear(GeneralBattle.I_WIN, threshold=0.8):
                logger.info("战斗赢,红色鼓")
                self.ui_click_until_disappear(GeneralBattle.I_WIN)
                break

            # 如果出现打败馆主的赢，就点击
            if self.appear(self.I_RYOU_DOKAN_WIN, threshold=0.8):
                logger.info("馆主的赢，就点击.")
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_WIN)
                return True

            # 如果出现失败 就点击，返回False。
            if self.appear(GeneralBattle.I_FALSE, threshold=0.8):
                logger.info("战斗失败，返回")
                self.ui_click_until_disappear(GeneralBattle.I_FALSE)
                break

            # 如果出现馆主战斗失败 就点击，返回False。
            if self.appear(self.I_RYOU_DOKAN_FAIL, threshold=0.8):
                logger.info("馆主战斗失败，返回")
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_FAIL)
                return False

            # 如果领奖励
            if self.appear(self.I_RYOU_DOKAN_BATTLE_OVER, threshold=0.6):
                logger.info("领奖励,那个魂")
                self.dokan_save_image()
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_BATTLE_OVER)
                break

            # 如果领奖励出现金币
            if self.appear(GeneralBattle.I_REWARD_GOLD, threshold=0.8):
                logger.info("领奖励,那个金币")
                self.dokan_save_image()
                self.ui_click_until_disappear(GeneralBattle.I_REWARD_GOLD)
                break

            # 如果开启战斗过程随机滑动
            if config.random_click_swipt_enable:
                logger.info("随机滑动....")
                logger.info("random swipt ...")
                self.random_click_swipt()

    def dokan_green_mark(self, enable: bool = False, mark_mode: GreenMarkType = GreenMarkType.GREEN_MAIN):
        """
        绿标， 如果不使能就直接返回
        :param enable:
        :param mark_mode:
        :return:
        """
        if enable:
            if self.dokan_wait_until_appear(self.I_GREEN_MARK, self.I_GREEN_MARK_1, wait_time=1):
                # logger.info("识别到绿标，返回")
                return
            else:
                logger.info("第一步识别绿标失败")
                # self.save_image(task_name="Dokan_greenmark_false_first", content="第一步识别绿标失败", push_flag=True, wait_time=0, image_type=True)
            # logger.info("Green is enable")
            x, y = None, None
            # logger.info(f"Green {mark_mode}")
            match mark_mode:
                case GreenMarkType.GREEN_LEFT1:
                    x, y = self.C_GREEN_LEFT_1.coord()
                case GreenMarkType.GREEN_LEFT2:
                    x, y = self.C_GREEN_LEFT_2.coord()
                case GreenMarkType.GREEN_LEFT3:
                    x, y = self.C_GREEN_LEFT_3.coord()
                case GreenMarkType.GREEN_LEFT4:
                    x, y = self.C_GREEN_LEFT_4.coord()
                case GreenMarkType.GREEN_LEFT5:
                    x, y = self.C_DOKAN_GREEN_LEFT_5.coord()
                case GreenMarkType.GREEN_MAIN:
                    x, y = self.C_GREEN_MAIN.coord()

            mark_timer = Timer(5)
            mark_timer.start()
            while 1:
                # 等待那个准备的消失
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_IN_FIELD)
                # 点击坐标
                self.device.click(x, y)
                if self.dokan_wait_until_appear(self.I_GREEN_MARK, self.I_GREEN_MARK_1, wait_time=1):
                    logger.info("识别到绿标,返回")
                    # self.save_image(task_name="Dokan_greenmark_ok", content="点击绿标成功", push_flag=True, wait_time=0, image_type=True)
                    break
                else:
                    logger.info("识别到绿标失败")
                    # self.save_image(task_name="Dokan_greenmark_false", content="识别绿标超时", push_flag=True, wait_time=0, image_type=True)
                if mark_timer.reached():
                    # logger.warning("识别绿标超时,返回")
                    break
                # 判断有无坐标的偏移
                # self.appear_then_click(self.I_LOCAL)

    def dokan_wait_until_appear(self, target, target2, wait_time) -> bool:
        wait_timer = Timer(wait_time)
        wait_timer.start()
        while 1:
            self.screenshot()
            if wait_timer and wait_timer.reached():
                logger.warning(f"Wait until appear {target.name} timeout")
                return False
            if self.appear(target) or self.appear(target2):
                return True

    def goto_dokan(self):

        if self.is_in_dokan():
            return True

        # 进入选择寮界面
        if self.ui_get_current_page() != page_guild:
            self.ui_goto(page_guild)

        while 1:
            self.screenshot()

            if self.appear_then_click(self.I_DAOGUAN, interval=1):
                continue
            if self.appear_then_click(self.I_GUILD_SHRINE, interval=1):
                continue
            if self.appear_then_click(self.I_GUILD_NAME_TITLE, interval=1):
                continue
            if self.appear(self.I_FANGSHOU, interval=1):
                break
            if self.appear(self.I_RYOU_DOKAN_CHECK, threshold=0.6):
                return

        while 1:
            self.screenshot()
            dokan_status_str = self.O_DOKAN_STATUS.detect_text(self.device.image)
            if dokan_status_str != '' and dokan_status_str is not None:
                break

        if '挑战成功' in dokan_status_str or '0次' in dokan_status_str:
            self.dokan_battle_number = 0
            self.check_current_weekday(True)
            if self.create_doukan_time:
                self.set_next_run(task='Dokan', target=self.create_doukan_time)
            else:
                self.set_next_run(task='Dokan', finish=True, server=True, success=True)
            self.finish_task()
        elif '集结中' in dokan_status_str:
            # 寮成员进入道馆
            self.dokan_quit = True
            self.goto_dokan_click()
            self.dokan_battle_number = 0
        else:
            if '2次' in dokan_status_str:
                self.dokan_quit = True
                self.dokan_battle_number = 2
            else:
                self.dokan_quit = False
                self.dokan_battle_number = 1
            # 管理开道馆
            if self.config.dokan.dokan_config.dokan_enable:
                self.open_dokan()
            else:
                self.goto_dokan_num += 1
                wait_time = 30
                logger.info(f"寮成员第{self.goto_dokan_num}次进入,等待{wait_time}秒, 管理开启道馆")
                time.sleep(wait_time)
                if self.goto_dokan_num >= 15:
                    logger.info(f"寮成员{self.goto_dokan_num}次未进入道馆, 结束任务!")
                    self.check_current_weekday(True)
                    if self.create_doukan_time:
                        self.set_next_run(task='Dokan', target=self.create_doukan_time)
                    else:
                        self.set_next_run(task='Dokan', finish=True, server=True, success=True)
                    self.finish_task()

    def goto_dokan_click(self):
        while 1:
            self.screenshot()

            if self.is_in_dokan():
                self.dokan_switch_soul()
                break
            if self.appear(GeneralInviteAssets.I_I_ACCEPT):
                continue
            if not self.open_welfare:
                # 遍历配置的福利寮标签关键字，在地图中查找匹配
                welfare_tag_list = [t.strip() for t in self.config.dokan.welfare_config.welfare_tags.split(',') if t.strip()]
                found_tag = False
                for tag in welfare_tag_list:
                    pos = self.O_DOKAN_MAP_2.ocr_full(self.device.image, keyword=tag)
                    if pos != (0, 0, 0, 0):
                        self.open_welfare = True
                        logger.info(f" find welfare tag: {tag}")
                        found_tag = True
                        break
                if not found_tag:
                    logger.info(f"failed to find welfare tags: {welfare_tag_list}")

            pos = self.O_DOKAN_MAP.ocr_full(self.device.image)
            if pos == (0, 0, 0, 0):
                logger.info(f"failed to find {self.O_DOKAN_MAP.keyword}")
            else:
                # 取中间
                x = pos[0] + pos[2] / 2
                # 往上偏移20
                y = pos[1] - 20
                # logger.info(f"ocr detect result pos={pos}, try click pos, x={x}, y={y}")
                self.device.click(x=x, y=y)
            
    def is_in_dokan(self):
        """
          判断是否在道馆里面
          :return:
          """
        self.screenshot()
        if self.appear(self.I_RYOU_DOKAN_CHECK, threshold=0.6):
            return True
        return False

    def find_dokan(self, con, welfare_flag):
        """
        寻找符合条件的道馆进行挑战。

        参数:
        score (float): 赏金与人数比值的阈值，默认为4.6。

        返回:
        bool: 是否找到了符合条件的道馆并进行挑战。
        """
        if welfare_flag:
            logger.info('开始寻找福利寮')
            self.find_dokan_list.append(f"─────────（开始寻找福利寮）─────────")
        else:
            logger.info('开始寻找普通寮')
            self.find_dokan_list.append(f"─────────（开始寻找普通寮）─────────")

        is_indokan, cur_scene = self.get_current_scene()
        if cur_scene != DokanScene.RYOU_DOKAN_SCENE_FINDING_DOKAN:
            return True

        # 刷新按钮点击次数
        num_fresh = 0
        # 备份一些重要的ROI区域，以便在循环中恢复
        backup = {'i_point_bounty': self.I_RIGHTPAD_POINT_BOUNTY.roi_back,
                  # 'o_dokan_rightpad_bounty':self.O_DOKAN_RIGHTPAD_BOUNTY.roi,
                  'i_point_people_num': self.I_CENTER_POINT_PEOPLE_NUMBER.roi_back}

        def restore_roi():
            self.I_RIGHTPAD_POINT_BOUNTY.roi_back = backup['i_point_bounty']
            self.I_CENTER_POINT_PEOPLE_NUMBER.roi_back = backup['i_point_people_num']

        # 当前屏最底部条目的寮名称（find_challengeable 写出，主循环做滑动末端检测）
        current_bottom_name: str = ''

        def find_challengeable(ignore_score=False):
            """
                查找当前列表状态(一般为4个)中符合条件的道馆,并点击使其显示挑战按钮
            @param ignore_score: 是否忽略道馆系数限制, - True:   那么选择当前列表状态系数最低的那个,点击显示挑战按钮
                                                   - False:  如果存在系数符合条件的,点击并显示挑战按钮
                                                            如果全部不符合条件,不进行任何操作,返回时,不显示挑战按钮
            @type ignore_score: float
            @return:
            @rtype:
            """
            nonlocal current_bottom_name
            restore_roi()
            self.screenshot()
            # 获取所有匹配结果并直接转换为所需格式
            raw_matches = self.I_RIGHTPAD_POINT_BOUNTY.match_all_any(image=self.device.image, roi=[1095,33,82,569])
            # 直接从匹配结果中提取坐标信息并按y坐标排序
            bounty_list = sorted(
                [[x, y, w, h] for (sc, x, y, w, h) in raw_matches],
                key=lambda item: item[1]  # 按y坐标排序
            )
            logger.info(f'find elements list:{bounty_list}')
            # 写出当屏最底部条目的寮名称，供主循环做「与上一屏相同即到底」的末端
            # 检测——金币图标每个条目都有，且坐标会因回弹/引擎物理轻微偏移，位置
            # 比对会漏判到底；寮名称是条目身份标识，不受像素级偏移影响
            if bounty_list:
                bottom_item = bounty_list[-1]
                self.O_DOKAN_RIGHTPAD_NAME.roi = self.position_offset(
                    (bottom_item[0], bottom_item[1], 150, 30), (-37, 29, 0, 0))
                current_bottom_name = self.O_DOKAN_RIGHTPAD_NAME.ocr(self.device.image)
            else:
                current_bottom_name = ''
            # 默认最小分数
            min_score = 10
            idx_selected = -1
            for idx, item in enumerate(bounty_list):
                self.device.click_record_clear()
                # logger.hr(f"开始识别道馆： No.{idx} = {item}", 2)
                self.O_DOKAN_RIGHTPAD_NAME.roi = self.position_offset((item[0], item[1], 150, 30), (-37, 29, 0, 0))
                self.O_DOKAN_RIGHTPAD_TAG.roi = self.position_offset((item[0], item[1], 40, 50), (20, -55, 0, 0))

                dokan_name = self.O_DOKAN_RIGHTPAD_NAME.ocr(self.device.image)
                dokan_tag = self.O_DOKAN_RIGHTPAD_TAG.ocr(self.device.image)
                """  # 添加截图功能，保存识别到道馆名称的区域
                roi_x, roi_y, roi_w, roi_h = self.O_DOKAN_RIGHTPAD_NAME.roi
                screenshot_name = f"道馆名称_{dokan_name}_position_{roi_x}_{roi_y}_{roi_w}_{roi_h}"
                self.save_image(task_name=screenshot_name, image_type=True, wait_time=0, push_flag=True, 
                               content=f'识别到道馆: {dokan_name}', custom_roi=(roi_x, roi_y, roi_w, roi_h)) """
                

                if dokan_name == "" or dokan_tag == "":
                    continue

                # 从配置中获取福利寮标签关键字列表
                welfare_tag_list = [t.strip() for t in self.config.dokan.welfare_config.welfare_tags.split(',') if t.strip()]
                tag_matched = dokan_tag in welfare_tag_list

                if (dokan_name in self.welfare_names or "鑫鑫子" in dokan_name) and tag_matched:
                    self.dokan_quit = True
                    self.open_welfare = True
                else:
                    if tag_matched:
                        self.push_notify(content=f'{dokan_name}')
                        self.config.notifier.push(content=f'{dokan_name}', title='发现未收录的福利寮')
                    # 如果是要开启福利寮，但是此寮不是福利寮，则跳过
                    if welfare_flag:
                        self.find_dokan_list.append(f"道馆: {dokan_name}, 不是福利寮")
                        continue

                # 点击使挑战按钮消失的区域(C_DOKAN_CANCEL_SELECT_DOKAN), 点击可能点击到其他寮,
                # 因此需要在此处多点几次,直到挑战按钮消失,
                # 又因为出现挑战按钮动画时长较长,因此需要耗时
                self.screenshot()
                while self.appear(self.I_CENTER_CHALLENGE):
                    self.click(self.C_DOKAN_CANCEL_SELECT_DOKAN, interval=1.5)
                    self.wait_animate_stable(self.C_DOKAN_CANCEL_SELECT_DOKAN_CHECK_ANIMATE, interval=0.5, timeout=1.5)

                # 扩大搜索区域,防止找不到
                self.I_RIGHTPAD_POINT_BOUNTY.roi_back = self.position_offset(item, (-10, -10, 20, 20))
                # Note: 道馆不可挑战时(被别的寮打了),8秒后跳过
                if not self.ui_click_until_appear_or_timeout(self.I_RIGHTPAD_POINT_BOUNTY, self.I_CENTER_CHALLENGE, interval=1.5, timeout=5):
                    logger.info(f"can't find challenge button,idx={idx} item={item}")
                    # 道馆不可挑战,挑战按钮不会弹出 ,直接进行下一个
                    continue

                # 获取防守人数
                self.screenshot()
                if not self.appear(self.I_CENTER_POINT_PEOPLE_NUMBER):
                    logger.warning(f"can't find point people number image, item={item}")
                    continue
                self.O_DOKAN_CENTER_PEOPLE_NUMBER.roi = self.position_offset(self.I_CENTER_POINT_PEOPLE_NUMBER.roi_front,(0, 0, 0, 30))
                p_num = self.O_DOKAN_CENTER_PEOPLE_NUMBER.detect_text(self.device.image)
                tmp = re.search(r"(\d+)", p_num)
                if not tmp:
                    logger.warning(f"can't find people number in ocr result,item={item}, p_num={p_num}")
                    continue
                p_num = int(tmp.group())

                # 最少人数随着刷新次数减少
                if num_fresh >= self.config.dokan.welfare_config.fresh_num_less_people:
                    min_people = con.min_people_num - num_fresh
                    min_people = max(min_people, 110)
                else:
                    min_people = con.min_people_num

                if p_num < min_people:
                    message = f"道馆: {dokan_name}, 人数:{p_num}, 不符合要求人数:{min_people}"
                    self.find_dokan_list.append(message)
                    logger.warning(message)
                    self.open_welfare = False
                    continue

                # 如果是要开启福利寮，且此寮人数校验已经通过，直接确认此寮
                if self.open_welfare:
                    message = f"✅ 开启福利道馆: {dokan_name}, 人数:{p_num}, 刷新次数:{num_fresh}"
                    self.find_dokan_list.append(message)
                    self.push_notify(content=message)
                    return True

                # 获取赏金金额
                self.O_DOKAN_RIGHTPAD_BOUNTY.roi = self.position_offset(item, (0, 0, 100, 0))
                bounty = self.O_DOKAN_RIGHTPAD_BOUNTY.ocr(self.device.image)
                tmp = re.search(r'(\d+)', bounty)
                if not tmp:
                    logger.warning(f"can't find bounty,item = {item},ocr bounty={bounty}")
                    continue
                bounty = int(tmp.group())
                
                item_score = float(f"{bounty / p_num:.2f}")
                dokan_info = (f"道馆: {dokan_name}, 资金: {bounty}, 人数: {p_num}, 系数: {item_score}")
                self.find_dokan_list.append(dokan_info)
                logger.info(f"========== {dokan_info} ==========")

                if item_score < min_score:
                    min_score = item_score
                    idx_selected = idx
                # 大于系数 或者 系数过小(文字识别错误导致)
                if item_score > con.find_dokan_score or item_score < 1.5:
                    logger.warning(f"系数{item_score}大于{con.find_dokan_score},不符合要求")
                    continue
                if bounty < con.min_bounty:
                    logger.warning(f"寮资金{bounty}少于{con.min_bounty},不符合要求")
                    continue
                # 道馆是否退出，来决定是否需要判断馆主等级
                if not self.dokan_quit:
                    # 馆主不是修习等级的
                    if not self.appear(self.I_CENTER_GUANZHU_XIUXI):
                        logger.warning(f"馆主不是修习等级的,不符合要求")
                        continue
                self.push_notify(content=f"开启道馆: {dokan_name},资金: {bounty},人数: {p_num},系数: {item_score}")
                return True
            # 在所有列表中都没有符合的,且忽略系数限制,那么就选择最低分数的那个,点击显示挑战按钮
            if ignore_score:
                x, y, w, h = bounty_list[idx_selected]
                while 1:
                    self.device.click(x, y)
                    sleep(0.5)
                    self.screenshot()
                    if self.appear(self.I_CENTER_CHALLENGE):
                        self.push_notify(content=f"选择当前列表中系数最低的{min_score}")
                        return True
            return False

        logger.hr("开始寻找合适的道馆", 2)
        while num_fresh < con.fresh_num:
            # 上一屏最底部条目的寮名称：None 表示本刷新轮第一屏（不参与到底比对）
            last_bottom_name = None
            # 逐个滑动查找：由「最底部条目寮名称与上一屏相同」的末端检测收敛——
            # 滑动向上滚时新内容只能从底部进入视野，底部条目身份不变即到底；
            # 滑动上限 4 次仅是滑动偶发不生效或名称识别抖动时的死滑兜底
            swipe_count = 0
            while True:
                sleep(3)
                if find_challengeable():
                    while 1:
                        self.screenshot()
                        if self.appear(self.I_RYOU_DOKAN_CHECK, interval=1):
                            break
                        if self.appear_then_click(self.I_CHALLENGE_ENSURE, interval=1):
                            continue
                        if self.appear_then_click(self.I_CENTER_CHALLENGE, interval=1):
                            continue
                    # 恢复初始位置信息,防止下次使用出错
                    restore_roi()
                    return True
                # 末端检测：最底部条目的寮名称与上一屏相同 → 没有新内容进入视野，
                # 列表已滚到底；两屏皆空同样视为到底（无目标可找，滑无意义）
                if last_bottom_name is not None:
                    both_empty = not current_bottom_name and not last_bottom_name
                    bottom_same = (current_bottom_name and last_bottom_name
                                   and current_bottom_name == last_bottom_name)
                    if both_empty or bottom_same:
                        logger.info(f'已滑到列表末端（最底部条目: {current_bottom_name or "空"}），停止滑动')
                        break
                # 滑动次数达到上限仍未到底，直接进入刷新
                if swipe_count >= 4:
                    break
                last_bottom_name = current_bottom_name
                self.swipe(self.S_DOKAN_LIST_UP, interval=1)
                swipe_count += 1
            # 恢复初始位置信息,防止下次使用出错
            restore_roi()
            num_fresh += 1
            logger.hr(f"第{num_fresh}次刷新列表", 2)
            self.find_dokan_list.append(f"─────────第{num_fresh}次刷新列表─────────")
            self.ui_click(self.C_DOKAN_REFRESH, self.I_REFRESH_ENSURE, interval=1)
            self.ui_click_until_disappear(self.I_REFRESH_ENSURE, interval=1)
            sleep(1)

        if welfare_flag:
            self.push_notify(content="未找到福利寮")
            return False

        # 刷新次数用完,仍未找到符合条件的道馆,选择当前列表(约4个)中系数最低的
        logger.warning("刷新次数已经上限,未找到符合条件的道馆,选择当前列表中系数最低的")
        if find_challengeable(ignore_score=True):
            while 1:
                self.screenshot()
                if self.appear(self.I_RYOU_DOKAN_CHECK, interval=1):
                    break
                if self.appear_then_click(self.I_CHALLENGE_ENSURE, interval=1):
                    continue
                if self.appear_then_click(self.I_CENTER_CHALLENGE, interval=1):
                    continue
            return True
        return False

    def open_dokan(self):
        # 判断是否需要建立道馆
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_RED_CLOSE, interval=1):
                continue
            if self.appear_then_click(self.I_CREATE_DAOGUAN_SURE, interval=1):
                time.sleep(3)
                continue
            if self.appear_rgb(self.I_CREATE_DAOGUAN_OK):
                break
            if self.appear_then_click(self.I_CREATE_DAOGUAN, interval=1):
                continue

        dokan_config = self.config.dokan.welfare_config
        if dokan_config.welfare_enable and self.find_dokan(dokan_config, welfare_flag=True):
            logger.info("已找到福利道馆")
        else:
            dokan_config = self.config.dokan.dokan_config
            self.find_dokan(dokan_config, welfare_flag=False)
            logger.info("已找到普通道馆")

        # 记录第一次开启道馆时间
        self.first_open_dokan_time = datetime.now()
        self.boss_battles = False
        # 道馆数量
        filtered_list = [item for item in self.find_dokan_list if "刷新列表" not in item]
        logger.info(f"总共查看道馆数量: {len(filtered_list)}")

        # 打印道馆列表
        i = 1
        for item in self.find_dokan_list:
            if "刷新列表" in item or "开始寻找" in item:
                i = 1
                logger.info(f"{item}")
                continue
            logger.info(f"Item {i}: {item}")
            i += 1
        self.find_dokan_list = []
        # 切换御魂
        self.dokan_switch_soul()

    def dokan_switch_soul(self, mode='normal'):
        """
        统一的切换御魂方法
        :param mode: 切换模式，'normal'为普通寮战（馆员），'master'为馆主战
        """
        # 更改式神录跳转
        ipages.page_shikigami_records.links.clear()
        # 式神录入口挂在 page_theme 下（卷轴展开后才点得到），劫持期间要摘掉的也是这条边
        if ipages.page_shikigami_records in ipages.page_theme.links:
            del ipages.page_theme.links[ipages.page_shikigami_records]
        ipages.page_shikigami_records.link(button=self.I_BACK_Y, destination=ipages.page_dokan)
        ipages.page_dokan.link(button=self.I_PAGE_DOKAN_GOTO_SHIKIGAMI_RECORDS, destination=ipages.page_shikigami_records)
        sleep(5)
        self.screenshot()
        if self.ui_get_current_page() != ipages.page_dokan:
            self.ui_goto(ipages.page_dokan)
        self.ui_goto_page(ipages.page_shikigami_records)
        
        cfg = self.config.dokan

        if mode == 'master':
            # 馆主战阵容御魂切换
            if cfg.switch_soul_config_master.enable:
                self.run_switch_soul(cfg.switch_soul_config_master.switch_group_team)
            if cfg.switch_soul_config_master.enable_switch_by_name:
                self.run_switch_soul_by_name(cfg.switch_soul_config_master.group_name, cfg.switch_soul_config_master.team_name)
        else:
            # 普通寮战阵容御魂切换
            if self.open_welfare:
                # 自动换御魂 福利寮
                if cfg.switch_soul_config2.enable:
                    self.run_switch_soul(cfg.switch_soul_config2.switch_group_team)
                if cfg.switch_soul_config2.enable_switch_by_name:
                    self.run_switch_soul_by_name(cfg.switch_soul_config2.group_name, cfg.switch_soul_config2.team_name)
            else:
                # 自动换御魂
                if cfg.switch_soul_config.enable:
                    self.run_switch_soul(cfg.switch_soul_config.switch_group_team)
                if cfg.switch_soul_config.enable_switch_by_name:
                    self.run_switch_soul_by_name(cfg.switch_soul_config.group_name, cfg.switch_soul_config.team_name)

        self.screenshot()
        self.ui_goto(ipages.page_dokan)
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
                # logger.warning(f"颜色匹配失败: [{target.name}]")
                return False

        logger.info(f"颜色匹配成功: [{target.name}]")
        return True

    def ui_click_until_appear_or_timeout(self, click, stop=None, interval: float = 1, timeout: float = 10):
        """
        在UI中点击某个元素，直到目标元素出现或达到超时时间。
        此函数主要用于自动化测试中，模拟用户点击操作，直到出现指定的界面元素或达到预设的超时时间。

        :param click: 要点击的元素规则，可以是图片规则、点击规则或OCR规则。
        :param stop: 可选参数，出现此元素时停止点击。如果为None，则一直点击直到超时。
        :param interval: 每次点击之间的间隔时间（秒）。默认为1秒。
        :param timeout: 总的超时时间（秒）。默认为10秒。
        :return: 如果在超时时间内找到目标元素，则返回True，否则返回False。
        """
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            sleep(0.3)
            self.screenshot()
            if self.appear(stop):
                return True
            if isinstance(click, RuleImage) and self.appear_then_click(click, interval=interval):
                continue
            if isinstance(click, RuleClick) and self.click(click, interval=interval):
                continue
            elif isinstance(click, RuleOcr) and self.ocr_appear_click(click, interval=interval):
                continue
        return False

    def find_all_element(self, item, offset: tuple) -> list[tuple[int, int, int, int]]:
        """
        NOTE: 仅适配查找道馆列表
       在当前对象中查找所有匹配的项目，并返回它们的信息列表。

       此函数的目的是通过循环搜索和匹配给定的项目，并将匹配的项目信息存储到一个列表中。
       如果项目出现，则将其添加到列表中，并根据预定义的规则调整项目的位置。

       参数:
       - item: 需要查找的项目。
       - offset: 如果当前区域查找不到,扩大查找区域的大小

       返回值:
       返回一个包含所有匹配项目信息的列表。
       """
        res_list = []
        while 1:
            if (item.roi_back[0] + item.roi_back[2] > (1280 + offset[2])) or (
                    item.roi_back[1] + item.roi_back[3] > (720 + offset[3])):
                break
            if self.appear(item):
                res_list.append(item.roi_front.copy())
                # 刷新搜索区域,使用上个搜索结果的Y坐标作为起始点的Y坐标,搜索结果的高度作为起始搜索高度
                item.roi_back = self.position_offset(item.roi_back, (
                    0, item.roi_front[1] + item.roi_front[3] - item.roi_back[1], 0,
                    item.roi_front[3] - item.roi_back[3]),
                                                     )
            item.roi_back = self.position_offset(item.roi_back, offset)
        return res_list

    def position_offset(self, src, offset: tuple):
        return (src[0] + offset[0], src[1] + offset[1]
                , src[2] + offset[2], src[3] + offset[3])

    def finish_task(self):
        # 恢复式神录跳转
        ipages.page_dokan.links.clear()
        if ipages.page_dokan in ipages.page_shikigami_records.links:
            del ipages.page_shikigami_records.links[ipages.page_dokan]
        # 恢复时同样挂回 page_theme：挂到 page_main 的话会绕过「先展开卷轴」这条必经路径
        ipages.page_theme.link(button=self.I_MAIN_GOTO_SHIKIGAMI_RECORDS, destination=ipages.page_shikigami_records)
        ipages.page_shikigami_records.link(button=self.I_BACK_Y, destination=ipages.page_main)

        # QQ群触发模式: 道馆完成后，下次运行设到21点
        cfg: Dokan = self.config.dokan
        if cfg.qq_group_trigger.enable:
            now = datetime.now()
            next_9pm = datetime.combine(now.date(), datetime.min.time().replace(hour=21))
            if now >= next_9pm:
                # 已过今天21点，设到明天21点
                next_9pm = datetime.combine((now + timedelta(days=1)).date(), datetime.min.time().replace(hour=21))
            logger.info(f"QQ群触发模式: 下次道馆运行时间设到 {next_9pm.strftime('%Y-%m-%d %H:%M')}")
            self.set_next_run(task='Dokan', target=next_9pm)

        raise TaskEnd('Dokan')
    def quit_battle(self):
        """
            尝试退出战斗界面
            1. 普通战斗结束,连续点击即可退出
                a. 存在战斗奖励
                b. 战斗失败,
            2. 在战斗界面,但是战斗还未开始(右下角有准备按钮)
                需要点击左上角退出按钮,然后点击确定
            3. 馆主战斗过程中,寮友打败馆主,弹出框体,可点击空白区域取消该框体.
            综上,点击左上角退出按钮区域
        """
        logger.info("try to quit battle...")
        while True:
            self.screenshot()
            if self.appear(self.I_RYOU_DOKAN_CENTER_TOP):
                break
            if self.appear(self.I_RYOU_DOKAN_QUIT_BATTLE_ENSURE):
                self.ui_click_until_disappear(self.I_RYOU_DOKAN_QUIT_BATTLE_ENSURE)
                break
            # 退出时,意外弹出"确认退出集结场景" 弹窗,导致卡住
            # 只能怀疑是上面两个appear的耗时 导致screenshot 过时导致错误点击
            # TODO: 待验证
            # 还有一种可能:在退出战斗过程中,在过场图出现前,点击左上角退出按钮,会导致该弹窗弹出(恶心)
            self.screenshot()
            if not self.appear(self.I_RYOU_DOKAN_CENTER_TOP):
                self.click(self.C_DOKAN_BATTLE_QUIT_AREA, interval=3)
                continue
            self.wait_until_appear(self.I_RYOU_DOKAN_CENTER_TOP, True, 3)


if __name__ == "__main__":
    """  from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = ScriptTask(c, d) """
    # t.save_image()
    # t.run()
    # t.dokan_switch_soul()
    #self.welfare_name_str()
    from dev_tools.assets_test import detect_image

    IMAGE_FILE = '.\\temp_path\\2026-03-10 22-16-40.png'    
    from tasks.Dokan.assets import DokanAssets
    from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
    """ jade = DokanAssets.I_RYOU_DOKAN_BATTLE_MASTER_FIRST
    jade.method = "Template matching"
    sign = DokanAssets.I_RYOU_DOKAN_BATTLE_MASTER_SECOND
    sign.method = "Template matching"
    pre = GeneralBattleAssets.I_PREPARE_HIGHLIGHT
    pre.method = "Template matching"
    detect_image(IMAGE_FILE, jade)
    detect_image(IMAGE_FILE, sign)
    detect_image(IMAGE_FILE, pre) """
    """
        查找当前列表状态(一般为4个)中符合条件的道馆,并点击使其显示挑战按钮
    @param ignore_score: 是否忽略道馆系数限制, - True:   那么选择当前列表状态系数最低的那个,点击显示挑战按钮
                                            - False:  如果存在系数符合条件的,点击并显示挑战按钮
                                                    如果全部不符合条件,不进行任何操作,返回时,不显示挑战按钮
    @type ignore_score: float
    @return:
    @rtype:
    """
    """ #restore_roi()
    self.screenshot()
    # 获取所有匹配结果并直接转换为所需格式
    raw_matches = self.I_RIGHTPAD_POINT_BOUNTY.match_all_any(image=self.device.image, roi=[1095,33,82,569])
    # 直接从匹配结果中提取坐标信息并按y坐标排序
    bounty_list = sorted(
        [[x, y, w, h] for (sc, x, y, w, h) in raw_matches],
        key=lambda item: item[1]  # 按y坐标排序
    )
    logger.info(f'find elements list:{bounty_list}')
    if len(bounty_list) < 4:
        self.save_image(task_name='搜索到的道馆少于4个', image_type=True, wait_time=0, push_flag=True, content='搜索到的道馆少于4个')
    # 默认最小分数
    min_score = 10
    idx_selected = -1
    for idx, item in enumerate(bounty_list):
        self.device.click_record_clear()
        # logger.hr(f"开始识别道馆： No.{idx} = {item}", 2)

        self.O_DOKAN_RIGHTPAD_NAME.roi = self.position_offset(item, (-37, 29, 127, 0))
        # 添加截图功能，保存识别到道馆名称的区域
        roi_x, roi_y, roi_w, roi_h = self.O_DOKAN_RIGHTPAD_NAME.roi
        screenshot_name = f"道馆名称_{dokan_name}_position_{roi_x}_{roi_y}_{roi_w}_{roi_h}"
        self.save_image(task_name=screenshot_name, image_type=True, wait_time=0, push_flag=True, 
                        content=f'识别到道馆: {dokan_name}', custom_roi=(roi_x, roi_y, roi_w, roi_h))
        dokan_name = self.O_DOKAN_RIGHTPAD_NAME.ocr(self.device.image) """
    # t.dokan_process(config.dokan)
    # t.find_dokan(config.dokan.welfare_config, True)
    # t.find_dokan()

    # welfare_names = t.welfare_name_str()
    # print(welfare_names)
    # if "锦鲤一一" in welfare_names:
    #     print("有")
    # else:
    #     print("没有")
    # test_ocr_locate_dokan_target()
    # test_anti_detect_random_click()
    # test_goto_main()

    # ===== 测试 find_dokan 寻找道馆流程 =====
    # 前提: 需先手动将游戏切换到「寻找道馆」界面(右侧道馆列表可见),
    #       否则 find_dokan 会在场景判断处直接返回。
    from module.config.config import Config
    from module.device.device import Device

    print("=" * 60)
    print("  find_dokan 测试 (配置: oas1)")
    print("=" * 60)

    c = Config('oas1')
    d = Device(c)
    self = ScriptTask(c, d)

    # 加载福利寮名单(find_dokan 内部会用到 self.welfare_names)
    self.welfare_names = self.welfare_name_str()

    # welfare_flag=True: 使用福利寮配置寻找福利寮
    # welfare_flag=False: 使用普通寮配置寻找普通寮
    # 按需切换下面两行其一进行测试
    result = self.find_dokan(c.dokan.welfare_config, welfare_flag=True)
    # result = self.find_dokan(c.dokan.dokan_config, welfare_flag=False)

    print("=" * 60)
    print(f"  find_dokan 返回: {result}")
    print("  道馆识别记录:")
    for line in self.find_dokan_list:
        print(f"    {line}")
    print("=" * 60)

    # ===== 以下为旧的 NapCat 道馆触发测试, 已注释保留 =====
    """
    from module.config.config import Config
    from module.device.device import Device

    print("=" * 60)
    print("  NapCat 道馆触发测试 (配置: oas3)")
    print("=" * 60)

    c = Config('oas1')
    d = Device(c)
    self = ScriptTask(c, d)

    test_cfg = c.dokan.qq_group_trigger

    if not test_cfg.enable:
        print("  [WARN] qq_group_trigger.enable = False，仍继续测试")

    print(f"  endpoint: {test_cfg.endpoint}")
    print(f"  group_id: {test_cfg.group_id}")
    print(f"  create_keyword: '{test_cfg.create_keyword}'")
    print(f"  create_sender_id: {test_cfg.create_sender_id}")
    print(f"  at_all_sender_id: {test_cfg.at_all_sender_id}")
    print(f"  require_at_all: {test_cfg.require_at_all}")
    print()

    # 1. 测试连通性
    print("--- 1. 测试 NapCat 连通性 ---")
    try:
        _h = {'Content-Type': 'application/json'}
        if test_cfg.access_token:
            _h['Authorization'] = f'Bearer {test_cfg.access_token}'
        _ep = test_cfg.endpoint if '//' in test_cfg.endpoint else f'http://{test_cfg.endpoint}'
        _r = requests.post(f'{_ep}/get_login_info', json={}, headers=_h, timeout=5)
        _d = _r.json()
        if _d.get('status') == 'ok':
            _info = _d.get('data', {})
            print(f"  [OK] NapCat 在线! QQ: {_info.get('user_id')}, 昵称: {_info.get('nickname')}")
        else:
            print(f"  [FAIL] API返回: {_d}")
    except Exception as e:
        print(f"  [FAIL] 无法连接: {e}")

    # 2. 拉取并打印群消息
    print()
    print("--- 2. 拉取群消息列表 ---")
    try:
        _ep = test_cfg.endpoint if '//' in test_cfg.endpoint else f'http://{test_cfg.endpoint}'
        _url = f'{_ep}/get_group_msg_history'
        _h = {'Content-Type': 'application/json'}
        if test_cfg.access_token:
            _h['Authorization'] = f'Bearer {test_cfg.access_token}'

        # 逐步增大拉取数量，直到覆盖到阈值时间之前的消息
        now = datetime.now()
        today_9pm = datetime.combine(now.date(), datetime.min.time().replace(hour=21))
        nine_pm_ts = today_9pm.timestamp()
        fetch_count = 50
        all_msgs = []
        last_msg_count = 0

        for attempt in range(10):
            _body = {'group_id': test_cfg.group_id, 'message_seq': 0, 'count': fetch_count}
            _r = requests.post(_url, json=_body, headers=_h, timeout=10)
            _data = _r.json()
            all_msgs = _data.get('data', {}).get('messages', [])

            # 去重
            _seen = set()
            _unique = []
            for m in all_msgs:
                mid = m.get('message_id')
                if mid not in _seen:
                    _seen.add(mid)
                    _unique.append(m)
            all_msgs = _unique
            current_count = len(all_msgs)

            valid_times = [m.get('time', 0) for m in all_msgs if m.get('time', 0) > 0]
            if not valid_times:
                break
            earliest = min(valid_times)

            if earliest < nine_pm_ts:
                print(f"  第{attempt+1}次拉取count={fetch_count}, "
                      f"最早={datetime.fromtimestamp(earliest).strftime('%H:%M:%S')}, 已覆盖到阈值时间之前")
                break
            if attempt > 0 and current_count <= last_msg_count:
                print(f"  第{attempt+1}次拉取count={fetch_count}, "
                      f"实际返回{current_count}条(与上次{last_msg_count}条相同), 无更多历史")
                break
            print(f"  第{attempt+1}次拉取count={fetch_count}, "
                  f"实际返回{current_count}条, "
                  f"最早={datetime.fromtimestamp(earliest).strftime('%H:%M:%S')}, 增大至{fetch_count+50}")
            last_msg_count = current_count
            fetch_count += 50
            if fetch_count > 1000:
                break

        print(f"  共获取 {len(all_msgs)} 条消息 (count={fetch_count})")
        print()

        # 打印每条消息的时间和内容
        for i, m in enumerate(all_msgs):
            t = m.get('time', 0)
            mid = m.get('message_id', 0)
            sender_id = m.get('sender', {}).get('user_id', 0)
            sender_nick = m.get('sender', {}).get('nickname', '')
            # 确保GBK终端可输出
            try:
                sender_nick.encode('gbk')
            except UnicodeEncodeError:
                sender_nick = sender_nick.encode('gbk', errors='replace').decode('gbk')
            ts_str = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S') if t > 0 else '(无时间)'

            # 提取文本内容
            content = m.get('message', '')
            text = ''
            has_at_all = False
            if isinstance(content, list):
                for seg in content:
                    if isinstance(seg, dict):
                        if seg.get('type') == 'text':
                            text += seg.get('data', {}).get('text', '')
                        elif seg.get('type') == 'at' and seg.get('data', {}).get('qq') == 'all':
                            has_at_all = True
                            text += '[CQ:at,qq=all]'
                        elif seg.get('type') == 'image':
                            text += '[图片]'
            elif isinstance(content, str):
                text = content
                if '[CQ:at,qq=all]' in text:
                    has_at_all = True

            text = text.strip()[:80]  # 截断过长消息
            # 确保文本在GBK终端可输出
            try:
                text.encode('gbk')
            except UnicodeEncodeError:
                text = text.encode('gbk', errors='replace').decode('gbk')
            at_flag = ' @all' if has_at_all else ''
            after_9pm = 'Y' if t >= nine_pm_ts else 'N'
            print(f"  [{i+1:3d}] {after_9pm} {ts_str} | msg_id={mid} | {sender_nick}({sender_id}){at_flag}")
            print(f"        {text}")
    except Exception as e:
        print(f"  [ERROR] {e}")

    # 3. 使用 ScriptTask 实例调用 check_qq_group_message
    print()
    print("--- 3. 测试道馆触发检测 ---")
    result = self.check_qq_group_message(test_cfg)

    print()
    print("=" * 60)
    if result:
        print("  >>> 结论: 满足道馆触发条件，应开启道馆任务!")
    else:
        print("  >>> 结论: 未满足触发条件，不开启道馆任务")
    print("=" * 60)
    """
    