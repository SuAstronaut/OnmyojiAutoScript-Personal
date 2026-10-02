# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import re
import time
import cv2
import numpy as np
from cached_property import cached_property
from datetime import timedelta, datetime
from typing import List
from module.base.timer import Timer
from module.atom.image_grid import ImageGrid
from module.logger import logger
from module.exception import TaskEnd

from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_guild , page_team,page_mall
from tasks.GameUi.assets import GameUiAssets
from tasks.DailyAltAcc.utils import GuildPopupMixin
from tasks.ReturnGift.assets import ReturnGiftAssets
from tasks.ReturnGift.config import ReturnGiftConfig
import random
import json
from pathlib import Path

from module.base.utils import point2str

SrMatch = tuple[float, int, int, int, int]


def parse_sr_count(text) -> int | None:
    # 解析OCR返回值中的SR碎片数量：DigitCounter返回元组(current, remain, total)时取第一个元素
    if text is None:
        return None
    if isinstance(text, tuple):
        if len(text) == 0:
            return None
        # DigitCounter识别不到内容时返回(0, 0, 0)，视为未识别而不是数量0
        # 真实数量为0时OCR结果形如"0/30"，total不为0，不会被误判
        if all(v == 0 for v in text):
            return None
        return int(text[0])
    text = str(text)
    if not text:
        return None
    target = text.split('/', 1)[0]
    match = re.search(r'\d+', target)
    if match:
        return int(match.group())
    match = re.search(r'\d+', text)
    if match:
        return int(match.group())
    return None


def sort_sr_matches(matches: list[SrMatch]) -> list[SrMatch]:
    # 固定按行列顺序处理匹配结果，保证输出稳定
    return sorted(matches, key=lambda item: (item[2], item[1]))


class ScriptTask(GuildPopupMixin, GameUi,ReturnGiftAssets):
    # 整个任务（送礼长循环/碎片统计导航）都在寮页面体系，突发弹窗跳过常开：
    # 寮友捐赠触发的「已收到碎片」/「感谢」模态弹窗在 screenshot() 入口统一关闭
    _guild_popup_active = True

    def run(self):
        con = self.config.return_gift.return_gift_config
        mode = con.return_gift_mode
        if mode == 'gift':
            return self._run_gift_mode()
        if mode == 'sr_count':
            return self._run_sr_count_mode()
        raise ValueError(f'Unsupported ReturnGift mode: {mode}')

    def _run_gift_mode(self):
        con = self.config.return_gift
        total_send_btn_click_count = 0

        timeout_duration = timedelta(hours=con.return_gift_config.return_gift_timeout.hour,
                                    minutes=con.return_gift_config.return_gift_timeout.minute,
                                    seconds=con.return_gift_config.return_gift_timeout.second)
        retry_count = 0
        timeout=datetime.now()
        self.screenshot()
        if self.ui_get_current_page() != page_guild:
            self.ui_goto(page_guild)

        if not self._check_daily_running_with_returngift():
            logger.info("无运行的Daily实例，等待3分钟...")
            daily_started = self._wait_and_monitor_daily_start(180, 10)
            if not daily_started:
                logger.info("等待3分钟后无Daily运行，ReturnGift跳过")
                next_run_time = datetime.now().replace(hour=0, minute=19, second=30, microsecond=0) + timedelta(days=1)
                self.set_next_run(task='ReturnGift', target=next_run_time)
                raise TaskEnd('ReturnGift')

        check_timer = Timer(45).start()
        while 1:
            self.screenshot()

            if check_timer.reached():
                check_timer.reset()
                if self._check_daily_running_with_returngift():
                    logger.info("检测到Daily运行，保持循环等待")
                    continue
                else:
                    logger.info("无Daily运行，ReturnGift跳过")
                    break

            if retry_count >= 4:
                retry_count=0
                if self.ui_get_current_page() != page_guild:
                    self.ui_goto(page_main)
                    self.ui_goto(page_guild)
                    continue
            """ if datetime.now() - self.start_time >= timeout_duration  or datetime.now() > timeout + timedelta(minutes=3):
                break """
            if self.appear(self.I_R_SEND_FLAG):
                sendtimeout, send_btn_click_count = self.send_gift()
                total_send_btn_click_count += send_btn_click_count
                if sendtimeout:
                    timeout=sendtimeout
                receivetimeout=self.receive_gift()
                if receivetimeout:
                    timeout=receivetimeout
                while 1:
                    self.screenshot()
                    if self.appear(self.I_R_PAGE_GUILD):
                        break
                    if self.appear_then_click(self.I_R_BACK_Y, interval=2):
                        continue
                    if not self.appear(self.I_TO_PAGE_PIECE) and self.ui_get_current_page() != page_guild:
                        self.ui_goto(page_guild)
                        continue
                    
                continue
            if self.appear_then_click(self.I_R_PAGE_GUILD,action=self.C_R_TOSEND_CLICK,interval=2):
                self.device.click_record_clear()
                time.sleep(1)
                continue
            retry_count += 1
        now = datetime.now()

        self.config.notifier.push(
            title='ReturnGift任务提醒',
            content=f'送礼按钮点击次数: {total_send_btn_click_count}'
        )

        next_run_time = now.replace(hour=0, minute=19, second=30, microsecond=0) + timedelta(days=1)
        self.set_next_run(task='ReturnGift', target=next_run_time)
        raise TaskEnd('ReturnGift')

    def _run_sr_count_mode(self):
        # 导航到回礼页面并统计碎片数量，模板资源使用预生成的静态文件
        self._goto_return_gift_page()
        self.count_sr_fragments()
        next_run_time = datetime.now().replace(hour=0, minute=19, second=30, microsecond=0) + timedelta(days=1)
        self.set_next_run(task='ReturnGift', target=next_run_time)
        raise TaskEnd('ReturnGift')

    def sr_count_output_path(self) -> Path:
        # 统计结果放在 config/tasks_config 目录，避免被 assets_extract 扫描到
        output = Path('config/tasks_config') / 'sr_count.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        return output

    def _get_sr_templates(self) -> list[tuple[str, RuleImage]]:
        # 收集所有 I_SR_* 资源，返回 (属性名, RuleImage) 列表
        templates = []
        for name in sorted(dir(self)):
            if name.startswith('I_SR_') and isinstance(getattr(self, name), RuleImage):
                templates.append((name, getattr(self, name)))
        return templates

    def build_sr_count_ocr(self, ocr_box) -> RuleOcr:
        # 动态OCR区域跟随SR模板命中位置，不写入assets.py
        return RuleOcr(roi=tuple(ocr_box), area=tuple(ocr_box), mode='DigitCounter', method='Default', keyword='', name='sr_count')

    def swipe_sr_count_page(self):
        self.device.click_record_clear()
        duration = 2
        p1 = (650, 520)
        p2 = (650, 350)
        logger.info('Swipe %s -> %s, %sS ' % (point2str(*p1), point2str(*p2), duration))
        # 改走 device.swipe 分派：跟随 control_method 配置并可进入拟人化路径（直调 swipe_adb 会绕过两者）
        self.device.swipe(p1, p2, duration=duration)

    def write_sr_count_result(self, items: list[dict]) -> list[dict]:
        """将SR碎片统计结果写入 config/tasks_config/sr_count.json，格式为 [{"name": "I_SR_X", "count": N}, ...]"""
        with open(self.sr_count_output_path(), 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        # 删除旧的发布队列，避免 DailyAltAcc 使用过期数据
        sr_cnt_file = Path('config/tasks_config/sr_cnt.json')
        if sr_cnt_file.exists():
            sr_cnt_file.unlink()
            logger.info(f'已删除旧的发布队列 {sr_cnt_file}')

        return items

    def count_sr_fragments(self) -> list[dict]:
        # OCR区域相对于模板匹配位置的固定偏移（由原 sr_count_ocr_box 与 sr_template_box 推导）
        OCR_DX = 8
        OCR_DY = 149
        OCR_W = 95
        OCR_H = 22
        EMPTY_SWIPE_LIMIT = 3

        templates = self._get_sr_templates()
        seen = set()
        items = []
        empty_swipes = 0

        while empty_swipes < EMPTY_SWIPE_LIMIT and len(seen) < len(templates):
            self.screenshot()
            found_new = False
            for attr_name, template in templates:
                if attr_name in seen:
                    continue
                # 使用资源自带的 roi_back 和 threshold 进行匹配
                matches = sort_sr_matches(template.match_all_any(image=self.device.image))
                if not matches:
                    continue
                score, x, y, w, h = matches[0]
                ocr_box = (x + OCR_DX, y + OCR_DY, OCR_W, OCR_H)
                # 滑动距离不足时图标已出现但下方数量区域仍在屏幕外，
                # 此时跳过计数，等下一轮滑动后数量完整显示再识别
                screen_h = self.device.image.shape[0]
                if ocr_box[1] + ocr_box[3] > screen_h:
                    logger.info(f'{attr_name} 碎片数量区域超出屏幕底部，待下一轮滑动后识别')
                    continue
                ocr = self.build_sr_count_ocr(ocr_box)
                count = None
                ocr_text = ''
                for _ in range(2):
                    # 同一命中先重试OCR，避免短暂识别失败导致当前可见SR被滑过
                    ocr_text = ocr.ocr(self.device.image)
                    count = parse_sr_count(ocr_text)
                    if count is not None:
                        break
                if count is None:
                    logger.warning(f'SR碎片数量OCR解析失败: {attr_name}, text={ocr_text}')
                    continue
                seen.add(attr_name)
                found_new = True
                items.append({
                    'name': attr_name,
                    'count': count,
                })
            if found_new:
                empty_swipes = 0
                continue
            self.swipe_sr_count_page()
            empty_swipes += 1

        return self.write_sr_count_result(items)

    def _goto_return_gift_page(self):
        # 统计模式只进入回礼页面，不执行送礼、收礼或Daily联动逻辑
        self.screenshot()
        if self.ui_get_current_page() != page_guild:
            self.ui_goto(page_guild)
        retry_count = 0
        while retry_count < 5:
            self.screenshot()
            if self.appear_then_click(self.I_TO_PAGE_PIECE, interval=1):
                time.sleep(1)
                continue
            if self.appear_then_click(self.I_PAGE_PIECE, interval=1):
                time.sleep(1.5)
                self.click(self.I_PAGE_PIECE)
                return 
            if self.appear_then_click(self.I_R_PAGE_GUILD, action=self.C_R_TOSEND_CLICK, interval=2):
                self.device.click_record_clear()
                time.sleep(1)
                continue                
            retry_count += 1
        raise RuntimeError('无法进入ReturnGift回礼页面')

    def position_offset(self, src, offset: tuple):
        return (src[0] + offset[0], src[1] + offset[1]
                , src[2] + offset[2], src[3] + offset[3])
    def is_piece_flag_equal_to_two(self, piece_flag: str) -> bool:
        """
        判断OCR识别结果是否包含数字2且不包含数字4
        """
        if not piece_flag:
            return False
            
        cleaned_flag = piece_flag.strip()
        return '2' in cleaned_flag and '4' not in cleaned_flag
    def get_roi_back_list(self):
        self.screenshot()
        # 获取所有匹配结果并直接转换为所需格式
        raw_matches = self.I_R_SEND_BTN.match_all_any(image=self.device.image, roi=[794,71,203,507])
        # 直接从匹配结果中提取坐标信息并按y坐标排序
        bounty_list = sorted(
            [[x, y, w, h] for (sc, x, y, w, h) in raw_matches],
            key=lambda item: item[1]  # 按y坐标排序
        )
        roilist=[]
        for idx, item in enumerate(bounty_list):
            self.O_PIECE_FLAG.roi = self.position_offset((item[0], item[1], 90, 50), (-215, 17, 0, 0))
            piece_flag = self.O_PIECE_FLAG.ocr(self.device.image)
            logger.info(f'piece_flag: {piece_flag}')
            if self.is_piece_flag_equal_to_two(piece_flag):
                roilist.append(list(self.position_offset((item[0], item[1], 200, 110), (-68, -43, 0, 0))))
        logger.info(f'匹配到的ROI列表: {roilist}')
        return roilist
    
    def send_gift(self):
        send_time=False
        send_btn_click_count = 0
        
        swipe_count = 0
        logger.info('开始送礼')
        while 1:
            retry_count = 0
            while 1:
                self.screenshot()
                if retry_count >= 3:
                    break 
                if self.appear_then_click(self.I_R_AWARD,action=self.C_R_AWARD_CLICK,interval=0.5):
                    retry_count = 0
                    self.device.click_record_clear()
                    continue
                roilist = self.get_roi_back_list()
                if not roilist:
                    retry_count +=1
                    continue
                self.I_R_SEND_BTN.roi_back = list(roilist[0]) 
                if self.appear_then_click(self.I_R_SEND_BTN, interval=0.5):
                    send_time=datetime.now()
                    self.device.click_record_clear()
                    send_btn_click_count += 1
                    retry_count = 0
                    continue
                retry_count +=1
                
            if  swipe_count >=2:
                break
            duration = 2
            safe_pos_x = random.randint(980, 1080)
            safe_pos_y = random.randint(500, 520)
            p1 = (safe_pos_x, safe_pos_y)
            p2 = (safe_pos_x, safe_pos_y - 300)
            logger.info('Swipe %s -> %s, %sS ' % (point2str(*p1), point2str(*p2), duration))
            # 同上：走 device.swipe 分派而非直调 swipe_adb
            self.device.swipe(p1, p2, duration=duration)
            swipe_count += 1
       
        return send_time, send_btn_click_count
    def receive_gift(self):
        send_time=False
        retry_count = 0
        while 1:
            self.screenshot()
            if retry_count > 5:
                break
            if self.appear_then_click(self.I_R_AWARD,action=self.C_R_AWARD_CLICK, interval=0.5):
                time.sleep(1)
                self.screenshot()
                self.appear_then_click(self.I_R_AWARD,action=self.C_R_AWARD_CLICK, interval=0.5)
                break
            if self.appear_then_click(self.I_R_RECEIVE_ENSURE, interval=0.5):
                retry_count = 0
                continue
            if self.appear_then_click(self.I_R_RECEIVE_BTN, interval=0.5):
                send_time=datetime.now()
                retry_count = 0
                continue
            if self.appear_then_click(self.I_R_TORECEIVE_FLAG2,action=self.C_R_TORECEIVE_FLAG2_CLICK, interval=0.5):
                retry_count = 0
                continue
            if not self.appear(self.I_R_TORECEIVE_FLAG2) and self.appear_then_click(self.I_R_TORECEIVE_FLAG,action=self.C_R_TORECEIVE_FLAG_CLICK, interval=1.5):
                retry_count = 0
                continue
            retry_count += 1
        while 1:
            self.screenshot()
            if retry_count > 5:
                self.ui_goto(page_guild)
                break
            if self.appear_then_click(self.I_R_BACK_RED, interval=0.5):
                retry_count = 0
                continue
            if self.appear(self.I_R_SEND_FLAG, interval=0.5):
                break
            retry_count += 1
        return send_time
    def _check_daily_running_with_returngift(self) -> bool:
        """检查是否有 total_returngift_enable=true 的Daily正在运行"""
        progress_file = Path('./config/tasks_config/daily_progress.json')
        if not progress_file.exists():
            return False

        with open(progress_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        for key, value in data.items():
            if value.get('status') != 'running':
                continue
            if value.get('returngift_enable', False):
                return True

        return False

    def _wait_and_monitor_daily_start(self, max_seconds=180, interval=10) -> bool:
        """等待并监控Daily启动，返回True=Daily已启动"""
        check_count = max_seconds // interval
        for i in range(check_count):
            if self._check_daily_running_with_returngift():
                logger.info("检测到Daily运行")
                return True
            logger.info(f"等待Daily启动... ({i * interval}/{max_seconds}s)")
            time.sleep(interval)
        return False
            
            


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('OAS1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.send_gift()
