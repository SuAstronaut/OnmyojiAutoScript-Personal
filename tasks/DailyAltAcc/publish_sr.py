# This Python file uses the following encoding: utf-8
import json
import re
import time
from pathlib import Path

from filelock import FileLock

from module.base.utils import point2str, save_image
from module.logger import logger
from tasks.DailyAltAcc.utils import DailyAltAccBase, guild_popup_guard
from tasks.ReturnGift.assets import ReturnGiftAssets
from tasks.ReturnGift.script_task import ScriptTask as ReturnGiftScriptTask

class PublishSr(DailyAltAccBase, ReturnGiftAssets):
    """发布SR碎片子功能：依据 sr_count.json 生成可发布队列，按序点击发布"""

    # 输入：碎片数量统计
    SR_COUNT_FILE = Path('config/tasks_config/sr_count.json')
    # 输出/续做：可发布次数队列，与输入统计文件放在同一目录
    SR_CNT_FILE = SR_COUNT_FILE.parent / 'sr_cnt.json'
    # 多实例读写队列文件时使用的进程级文件锁
    SR_CNT_LOCK_FILE = Path(str(SR_CNT_FILE) + '.lock')
    # 一次发布需要消耗的碎片数
    PER_PUBLISH = 99
    # 滑动后等待列表惯性滚动停止的时间，避免模板匹配到漂移中的坐标
    SWIPE_SETTLE_WAIT = 1.5
    # 阶段1（点击碎片进入发布页）超时；取消祈愿后列表重排需要更多时间找回目标
    ENTER_PUBLISH_TIMEOUT = 8
    # 阶段1连续未命中多久后尝试滑动找回目标
    ENTER_PUBLISH_SWIPE_AFTER = 3
    # 阶段1为找回目标最多滑动的次数
    ENTER_PUBLISH_SWIPE_LIMIT = 2

    @guild_popup_guard
    def run_publish_sr(self):
        """发布SR碎片入口：已有队列则续做，否则从统计文件构建"""
        if self.SR_CNT_FILE.exists():
            queue = self._read_queue()
        else:
            queue = self._build_queue_from_sr_count()
            self._write_queue(queue)
        ReturnGiftScriptTask._goto_return_gift_page(self)
        self._publish_loop(queue)

    def _build_queue_from_sr_count(self) -> list[dict]:
        """读取 config/tasks_config/sr_count.json，将 count 转换为可发布次数(count // 99)"""
        if not self.SR_COUNT_FILE.exists():
            logger.warning(f'{self.SR_COUNT_FILE} 不存在，发布队列为空')
            return []
        with open(self.SR_COUNT_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        items = []
        for entry in raw:
            publish_times = int(entry['count']) // self.PER_PUBLISH
            if publish_times <= 0:
                continue
            items.append({'name': entry['name'], 'count': publish_times})
        return self._sort_queue(items)

    def _sort_queue(self, queue: list[dict]) -> list[dict]:
        """按 count 降序排序；count 相同时原列表中靠后的项排在前面"""
        indexed = list(enumerate(queue))
        indexed.sort(key=lambda iv: (-iv[1]['count'], -iv[0]))
        return [v for _, v in indexed]
    def _screenshot_sr(self):
        """进入祈愿(碎片)页保存截图供人工核查；发布失败时先关掉残留弹窗再导航"""
        retry_count = 0
        screenshot_flag = False
        while retry_count < 5:
            self.screenshot()
            if screenshot_flag == True and self.appear_then_click(self.I_UI_BACK_RED,interval=1):
                return True
            # 发布失败可能停在「取消发布」二次确认弹窗，先确认取消回到碎片列表
            if self.appear(self.I_PAGE_PUBLISH_CANCEL):
                self.ui_click_until_disappear(self.I_PUBLISH_CANCEL_ENSURE, interval=1)
                continue
            # 发布失败也可能停在碎片发布详情页，用红色返回键退出
            if self.appear(self.I_PAGE_PUBLISH) and self.appear_then_click(self.I_H_BACK_RED, interval=1):
                time.sleep(1)
                continue
            if self.appear_then_click(self.I_TO_PAGE_PIECE, interval=1):
                time.sleep(1)
                continue
            if self.appear_then_click(self.I_PAGE_PIECE, interval=1):
                time.sleep(1.5)
                image = self.screenshot()
                # 角色名优先取多账号运行注入的统计上下文(_stat_ctx)，单实例运行时退化为配置实例名
                char_name = (getattr(self, '_stat_ctx', None) or {}).get('char') or self.config.config_name
                # 替换 Windows 文件名非法字符，避免保存失败
                char_name = re.sub(r'[\\/:*?"<>|]', '_', str(char_name))
                save_dir = Path('screenshots/SR_Screenshots')
                save_dir.mkdir(parents=True, exist_ok=True)
                save_path = save_dir / f'{char_name}.png'
                save_image(image, str(save_path))
                logger.info(f'SR碎片页截图已保存: {save_path}')
                screenshot_flag = True
                continue
            if self.appear_then_click(self.I_R_PAGE_GUILD, action=self.C_R_TOSEND_CLICK, interval=2):
                self.device.click_record_clear()
                time.sleep(1)
                continue                
            retry_count += 1
        return False
        
    def _publish_loop(self, queue: list[dict]):
        """发布主循环：遍历队列匹配模板 → 点击 → 发布 → 递减 → 回写；未命中时滑动翻页直到连续空滑到底"""
        EMPTY_SWIPE_LIMIT = 3
        empty_swipes = 0
        while queue:
            matched_index = self._find_first_match(queue)
            if matched_index is None:
                empty_swipes += 1
                if empty_swipes >= EMPTY_SWIPE_LIMIT:
                    logger.info('连续滑动未命中，已到底部，结束发布流程')
                    break
                self._swipe_page()
                continue

            empty_swipes = 0
            top = queue[matched_index]
            publish_success = self._do_publish_sr(top['name'])

            if publish_success:
                top['count'] -= 1
                if top['count'] <= 0:
                    queue.pop(matched_index)
                queue = self._sort_queue(queue)
                self._write_queue(queue)
            else:
                logger.warning(f'发布 SR 碎片: {top["name"]} 失败，进入祈愿页保存当前状态截图')

            # 无论成功失败，都进入祈愿页截图供人工核查
            self._screenshot_sr()
            time.sleep(1)
            break
    def _find_first_match(self, queue: list[dict]) -> int | None:
        """截图一次后遍历队列，返回首个命中模板的索引；全部未命中返回 None"""
        self.screenshot()
        for idx, entry in enumerate(queue):
            rule = getattr(ReturnGiftAssets, entry['name'], None)
            if rule is None:
                logger.warning(f'ReturnGiftAssets 中找不到 {entry["name"]}，跳过')
                continue
            if self.appear_then_click(rule):
                return idx
        return None

    def _swipe_page(self):
        """向下滑动碎片列表页；滑动后列表存在惯性滚动，需等画面稳定再匹配"""
        self.device.click_record_clear()
        p1 = (650, 520)
        p2 = (650, 350)
        logger.info('Swipe %s -> %s, 2S' % (point2str(*p1), point2str(*p2)))
        # 改走 device.swipe 分派：跟随 control_method 配置并可进入拟人化路径（ReturnGift 同步修改）
        self.device.swipe(p1, p2, duration=2)
        # 惯性未停时模板匹配到的坐标会漂移，点击会落空，故等待列表停稳
        time.sleep(self.SWIPE_SETTLE_WAIT)

    def _do_publish_sr(self, name: str):
        """执行单次SR碎片发布"""
        self.screenshot()
        rule = getattr(self, name, None)
        if rule is None:
            logger.warning(f'找不到资源 {name}，跳过发布')
            return False
        logger.info(f'发布 SR 碎片: {name}')
        start_time = time.time()
        # 取消祈愿后列表会重排、目标可能滚出可见区域，故超时放宽并支持滑动找回
        last_seen_time = time.time()
        swipe_count = 0
        while (time.time()-start_time < self.ENTER_PUBLISH_TIMEOUT):
            self.screenshot()
            if self.appear(self.I_PAGE_PUBLISH):
                logger.info(f'选择发布个数: {name}')
                break
            if self.appear(self.I_PAGE_PUBLISH_CANCEL):
                self.ui_click_until_disappear(self.I_PUBLISH_CANCEL_ENSURE,interval=1)
                ReturnGiftScriptTask._goto_return_gift_page(self)
                # 重新导航回碎片页后列表仍在渲染，等稳定再匹配
                time.sleep(self.SWIPE_SETTLE_WAIT)
                start_time = time.time()
                last_seen_time = time.time()
                swipe_count = 0
                continue
            if self.appear_then_click(rule,interval=1):
                start_time = time.time()
                last_seen_time = time.time()
                continue
            # 连续匹配不到目标：滑动翻页找回，避免在空白页面干等到超时
            if (time.time()-last_seen_time >= self.ENTER_PUBLISH_SWIPE_AFTER
                    and swipe_count < self.ENTER_PUBLISH_SWIPE_LIMIT):
                logger.info(f'{name} 连续未命中，滑动列表重试({swipe_count + 1}/{self.ENTER_PUBLISH_SWIPE_LIMIT})')
                self._swipe_page()
                swipe_count += 1
                last_seen_time = time.time()
                start_time = time.time()
                continue
        if time.time()-start_time >= self.ENTER_PUBLISH_TIMEOUT:
            return False
        start_time = time.time()
        while (time.time()-start_time < 5):
            self.screenshot()
            # 数量已到 99：只点击确认按钮关闭数量面板，面板消失后结束本阶段。
            # 不能用 ocr_appear_click(interval=1)：限流时确认被跳过、误落到 ADD_COUNT；
            # 面板关闭后 ADD_COUNT 位置为空，空点击会取消发布，PUBLISH_ENSURE 也不再出现
            if self.ocr_appear(self.O_CHECK_COUNT):
                if not self.appear(self.I_PUBLISH_ENSURE2):
                    break
                if self.appear_then_click(self.I_PUBLISH_ENSURE2, interval=1):
                    start_time = time.time()
                continue
            # 数量未到 99：打开数量面板并逐次增加数量
            if self.appear_then_click(self.I_ADD_COUNT,interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_TO_SELECT_COUNT,interval=1):
                start_time = time.time()
                continue
        if time.time()-start_time >= 5:
            return False
        start_time = time.time()
        while (time.time()-start_time < 5):
            self.screenshot()
            if not self.appear(self.I_PAGE_PUBLISH) and not self.appear(self.I_PUBLISH_ENSURE):
                logger.info(f'发布 SR 碎片: {name} 成功')
                break
            if self.appear_then_click(self.I_PUBLISH_ENSURE,interval=1):
                start_time = time.time()
                continue
        if time.time()-start_time >= 5:
            return False
        return True
        
        
    def _read_queue(self) -> list[dict]:
        """读取运行期队列文件，使用文件锁避免多实例读到半写入内容"""
        with FileLock(str(self.SR_CNT_LOCK_FILE)):
            with open(self.SR_CNT_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)

    def _write_queue(self, queue: list[dict]):
        """写入运行期队列文件，使用文件锁支持多实例安全读写"""
        self.SR_CNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.SR_CNT_LOCK_FILE)):
            with open(self.SR_CNT_FILE, 'w', encoding='utf-8') as f:
                json.dump(queue, f, ensure_ascii=False, indent=2)
