from cached_property import cached_property

from module.atom.animate import RuleAnimate
from module.atom.image import RuleImage
from module.base.timer import Timer
from module.logger import logger
from tasks.Exploration.base import BaseExploration, Scene
from tasks.Exploration.version import HighLight


class ExploreWantedBoss(Exception):
    # 出现一种情况，要求的怪是仅仅最后的Boss，其他小怪不是
    pass


class WQExplore(BaseExploration, HighLight):
    _cnt_exploration: int = 0

    @cached_property
    def _match_end(self):
        return RuleAnimate(self.I_SWIPE_END)

    def explore(self, goto: RuleImage, num: int):
        logger.info(f'Start exploring with number: {num}')
        explore_init = False
        explore_only_boss: bool = True
        _cnt_exploration = 0
        search_fail_cnt = 0
        # 光圈持续变化，至少连续三帧未命中且等待超过滑动时长，才移动或判断末尾。
        search_fail_timer = Timer(3.2, count=2)
        while 1:
            self.screenshot()
            if self.appear(self.I_UI_BACK_YELLOW) and self.appear(self.I_E_EXPLORATION_CLICK):
                break
            if self.appear_then_click(goto, interval=2):
                continue

        while 1:
            scene = self.get_current_scene(reuse_screenshot=False)
            # 过场、进章和战斗耗时不属于连续找怪失败，回到探索后重新采样。
            if scene != Scene.MAIN:
                search_fail_timer.clear()
            # 进入探索
            if scene == Scene.ENTRANCE:
                if _cnt_exploration >= num:
                    logger.info('Execution exploration end')
                    # --退出到探索大世界--
                    self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_CHECK_EXPLORATION, interval=4)
                    if explore_only_boss:
                        raise ExploreWantedBoss
                    break
                self.ui_click(self.I_E_EXPLORATION_CLICK, stop=self.I_E_SETTINGS_BUTTON)
                continue
            # 探索大世界
            elif scene == Scene.WORLD:
                self.wait_until_stable(
                    self.I_CHECK_EXPLORATION,
                    timer=Timer(limit=0.5, count=2),
                    timeout=Timer(2, count=10)
                )
                curr_scene = self.get_current_scene(reuse_screenshot=False)
                if curr_scene == Scene.WORLD:
                    # 已离开章节就结束本批次，由上层重读悬赏进度并按需再次前往；
                    # 预估次数未满也不能在大世界等待已消失的章节入口。
                    logger.info(f'Exploration returned to world: {_cnt_exploration}/{num}')
                    if explore_only_boss:
                        # 与入口分支保持一致，整批没打到目标时仍排除无效路线。
                        raise ExploreWantedBoss
                    break
                continue
            # 探索里面
            elif scene == Scene.MAIN:
                # if not explore_init:
                #     count = 0
                #     while count < 5:
                #         if self.appear(self.I_E_AUTO_ROTATE_ON):
                #             break
                #         if self.appear(self.I_E_AUTO_ROTATE_OFF, interval=1.5):
                #             self.click(self.I_E_AUTO_ROTATE_OFF)
                #             count += 1
                #     explore_init = True
                #     continue
                # 小纸人
                if self.appear(self.I_BATTLE_REWARD):
                    if self.ui_get_reward(self.I_BATTLE_REWARD):
                        # 领奖后的首帧可能仍在过渡，不能沿用领奖前的失败计时。
                        search_fail_timer.clear()
                        continue
                # boss
                if self.appear(self.I_BOSS_BATTLE_BUTTON):
                    if self.fire(self.I_BOSS_BATTLE_BUTTON):
                        logger.info(f'Boss battle, minions cnt {self.minions_cnt}')
                        _cnt_exploration += 1
                        explore_only_boss = False
                    # 已经发现目标，点击等待和战斗时间都不能触发后续空扫。
                    search_fail_timer.clear()
                    continue
                # 小怪沿用当前动态高亮规则；识别后等待点击，不额外引入多尺度底层。
                if self.appear(self.TEMPLATE_GIF):
                    if self.fire(self.TEMPLATE_GIF):
                        explore_only_boss = False
                        logger.info(f'Fight, minions cnt {self.minions_cnt}')
                    # 识别到高亮但尚未点进战斗时也先重试，不能当作未找到怪而滑走。
                    search_fail_timer.clear()
                    continue
                # 第一帧未命中先启动计时，避免未启动的 Timer 直接被判为超时。
                if not search_fail_timer.started():
                    search_fail_timer.start()
                # 只在连续多帧找不到目标时向后拉，不要求人物和光圈停止动画。
                if search_fail_timer.reached():
                    if self._match_end.stable(self.device.image, refresh_after_stable=True):
                        _cnt_exploration += 1
                        self.quit_explore()
                        # 退出过程可能耗时，下一轮找怪重新计时。
                        search_fail_timer.clear()
                        continue
                    self.swipe(self.S_SWIPE_BACKGROUND_RIGHT, interval=3)
                    # swipe 没有返回值；调用结束后重置，保留新位置的完整采样窗口。
                    search_fail_timer.reset()
                    continue
            elif scene == Scene.BATTLE_PREPARE:
                self.ui_click_until_disappear(self.I_PREPARE_HIGHLIGHT, interval=0.5)
            elif scene == Scene.UNKNOWN:
                continue


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    from tasks.WantedQuests.assets import WantedQuestsAssets

    config = Config('oas1')
    device = Device(config)
    t = WQExplore(config, device)
    t.explore(goto=WantedQuestsAssets.I_GOTO_1, num=2)