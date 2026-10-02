import math
import time

import cv2
import numpy as np
from module.atom.ocr import RuleOcr
from module.atom.click import RuleClick
from module.atom.gif import RuleGif
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.exception import GameNotRunningError
from module.logger import logger
from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.character_match import (
    CHARACTER_LIST_EMPTY_OCR_DELAY, CHARACTER_LIST_EMPTY_OCR_LIMIT,
    find_character_index, is_character_name,
)
from tasks.Component.SwitchAccount.dropdown_state import is_account_dropdown_opened
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.base_task import BaseTask
def _prepare_image_for_ocr_1(image: np.ndarray, asset: RuleOcr) -> np.ndarray:
    """
    入参：image(np.ndarray/cv2 BGR格式)、asset(RuleOcr含ROI)
    返回：np.ndarray处理后图片
    核心逻辑：仅保留ROI内邮箱区域，擦除该区域内其他所有内容，原图非ROI区域不变
    """
    # 深拷贝原图，避免修改原始输入数据
    img_process = image.copy()
    # 提取ROI坐标并裁剪出目标区域（仅处理ROI内内容）
    roi_x, roi_y, roi_w, roi_h = asset.roi
    roi_area = img_process[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w]
    
    # 防护：若ROI裁剪后为空，直接返回原图
    if roi_area.size == 0:
        return img_process

    # 1. ROI转灰度图（兼容3通道BGR/单通道灰度图）
    if len(roi_area.shape) == 3 and roi_area.shape[2] == 3:
        gray = cv2.cvtColor(roi_area, cv2.COLOR_BGR2GRAY)
    else:
        gray = roi_area

    # 2. OTSU自动阈值反相二值化（自适应亮度，精准提取文字轮廓）
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    # 3. 横向膨胀（连接邮箱零散文字轮廓，确保检测到完整邮箱区域）
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (6, 2))
    binary_dilate = cv2.dilate(binary, kernel, iterations=1)

    # 4. 查找外部轮廓（兼容所有OpenCV版本）
    contours, _ = cv2.findContours(binary_dilate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # 5. 先将整个ROI填充为白色（擦除所有内容）
    roi_area[:] = (255, 255, 255)

    # 6. 筛选邮箱轮廓，将原邮箱内容还原回去（仅保留邮箱）
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        # 邮箱特征：水平长条形（可根据实际图片微调）
        if w >= 30 and h >= 8 and (w / h) >= 4:
            # 从原始ROI中截取邮箱区域，还原到白色ROI中
            roi_area[y:y+h, x:x+w] = image[roi_y+y:roi_y+y+h, roi_x+x:roi_x+x+w]

    # 7. 将处理后的ROI写回原图，返回结果
    img_process[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w] = roi_area
    return img_process

class LoginAccount(BaseTask, SwitchAccountAssets):

    def get_svr_name(self):
        self.screenshot()
        ocrRes = self.O_SA_LOGIN_FORM_SVR_NAME.ocr(self.device.image)
        return ocrRes

    def switch_svr(self, svrName: str):
        """
            需保证账号已登录 且处于登录界面
        @param svrName:
        @type svrName:
        """
        # 服务器名与角色名一样存在异体字：预设「猫川別馆」与表单读到的「猫川别馆」
        # 必须视为同一服务器，统一归一成「别」再做严格相等比较
        self.O_SA_LOGIN_FORM_SVR_NAME.keyword = svrName.replace('別', '别')
        if self.ocr_appear(self.O_SA_LOGIN_FORM_SVR_NAME):
            return True
        # 单独切服时显式传区服字段，不能把区服当成角色名。
        return self.switch_character("", svrName)
        """ self.ui_click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, self.I_SA_CHECK_SELECT_SVR_1, 1.5)
        # 展开底部角色列表,显示角色所属服务器
        self.screenshot()
        if self.appear(self.I_SA_CHECK_SELECT_SVR_1) and (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)):
            self.click(self.O_SA_SELECT_SVR_CHARACTER_LIST)

        self.O_SA_SELECT_SVR_SVR_LIST.keyword = svrName
        found = False
        lastSvrList: tuple = ()
        while 1:
            self.screenshot()
            # 灰度图
            self.device.image = cv2.cvtColor(self.device.image, cv2.COLOR_BGR2GRAY)
            # ret, self.device.image = cv2.threshold(self.device.image, 200, 255, cv2.THRESH_OTSU)
            ret, self.device.image = cv2.threshold(self.device.image, 100, 255, cv2.THRESH_BINARY)
            # self.device.image = cv2.adaptiveThreshold(self.device.image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 10)
            self.device.image = abs(255 - self.device.image)

            # RGB图
            self.device.image = cv2.cvtColor(self.device.image, cv2.COLOR_GRAY2RGB)

            ocrRes = self.O_SA_SELECT_SVR_SVR_LIST.detect_and_ocr(self.device.image)
            # 受限于图像识别文字准确率,此处对识别结果与实际服务器名字 进行检查 字重合度大于阈值 就认为查找成功
            thresh = 0.5
            ocrSvrList = [res.ocr_text for res in ocrRes]
            for index, ocrSvrName in enumerate(ocrSvrList):
                if len(ocrSvrName) < 3:
                    break
                tmp = set(svrName).intersection(set(ocrSvrName))
                if len(tmp) > max(len(svrName), len(ocrSvrName)) * thresh:
                    logger.info("found svr %s which is similar with %s", ocrSvrName, svrName)
                    found = True
                    # 确定点击位置
                    box = ocrRes[index].box
                    self.O_SA_SELECT_SVR_SVR_LIST.area = [self.O_SA_SELECT_SVR_SVR_LIST.roi[0] + box[0][0],
                                                          self.O_SA_SELECT_SVR_SVR_LIST.roi[1] + box[0][1],
                                                          box[1][0] - box[0][0],
                                                          box[2][1] - box[1][1]]
                    # 跳出此层for循环
                    break
            # 两次OCR结果相等表示滑动到最右侧
            if found or lastSvrList == ocrSvrList:
                break
            lastSvrList = ocrSvrList
            self.swipe(self.S_SA_SVR_SWIPE_LEFT)
            time.sleep(3.5)
        if found:
            self.click(self.O_SA_SELECT_SVR_SVR_LIST, interval=1.5)
            return True
        # 没找到 点击空白区域关闭选择服务器界面
        self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
        return False """

    @classmethod
    def _is_character_name(cls, ocr_text: str, characterName: str) -> bool:
        """判断一条 OCR 文本是否就是目标角色名。

        角色名左侧有一个圆形等级徽章，PP-OCRv6 识别率比旧引擎高，
        会把徽章里的等级数字读进同一个文本框，例如目标 角色A
        实际读出 '60角色A' 或 '60 角色A'（数字与名字之间可能
        带空格），严格相等比较就永远匹配不上。

        从后往前匹配：先确认文本以目标角色名结尾（名字是文本的后缀），
        再切断名字部分，检查剩余前缀是否符合等级格式——空（角色名本身
        以数字开头时不被误剥）或 1~2 位数字（可带一个尾随空格，兼容
        '60 角色A' 这种检测框拆开的情况）。前缀过长或非数字一律拒绝，
        避免把 角色B 之类的相邻角色误判成目标。

        同时统一异体字：游戏内显示「瑤/別」而配置里通常写「瑶/别」。
        """
        # 两套登录界面共用同一判据，空目标不会再匹配任意 OCR 文本。
        return is_character_name(ocr_text, characterName)

    @staticmethod
    def _bottom_ocr_texts(ocr_results) -> tuple[str, ...]:
        """提取 OCR 结果中最下面一组文字，用于判断列表是否已经滑到底部。

        每张卡片按区服、登录时间、角色名从上到下排列，最底一行通常是并排卡片的
        角色名。按检测框纵向重叠归组，只比较这一行可避开上方登录时间的动态变化。
        """
        if not ocr_results:
            return ()

        def box_bounds(item):
            box = item.box
            ys = [point[1] for point in box]
            return min(ys), max(ys)

        bottom_item = max(ocr_results, key=lambda item: box_bounds(item)[1])
        bottom_top, bottom_bottom = box_bounds(bottom_item)
        bottom_group = []
        for item in ocr_results:
            item_top, item_bottom = box_bounds(item)
            if item_top <= bottom_bottom and item_bottom >= bottom_top:
                bottom_group.append(item)

        # 统一按横坐标排序，避免 OCR 返回顺序变化导致相同底部文字被误判不同
        bottom_group.sort(key=lambda item: min(point[0] for point in item.box))
        return tuple(item.ocr_text for item in bottom_group)

    def switch_character(self, characterName: str, svrName: str = ""):
        """
              需保证账号已登录 且处于登录界面
        @param characterName:
        @param svrName: 区服名在同一屏里唯一，优先按它定位；未命中才回落角色名
        @return:
        @rtype:
        """
        # 未指定任何目标时不能打开列表并默认点击第一个角色。
        if not characterName.strip() and not svrName.strip():
            return False
        logger.info("start switch_character")
        last_alive_check = time.monotonic()
        # 只在新阶段开始时清理旧记录，后续重复点击必须保留卡死检测能力。
        self.device.click_record_clear()
        self.device.stuck_record_clear()
        while 1:
            self.screenshot()
            if self.appear(self.I_SA_CHECK_SELECT_SVR_1):
                break
            now = time.monotonic()
            # 点击切服按钮过程中游戏可能闪退回MuMu桌面，每隔10秒复用现成逻辑检测一次，若已闪退则抛异常交由恢复处理
            if now - last_alive_check >= 10:
                self._ensure_game_alive()
                last_alive_check = now
            self.click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, interval=1.5)
        # 展开底部角色列表,显示角色所属服务器
        """ self.screenshot()
        while (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)) and self.appear(self.I_SA_CHECK_SELECT_SVR_1):
            logger.info("open svr icon")
            self.click(self.C_SA_SELECT_SVR_CHARACTER_LIST, interval=1.5)
            self.wait_until_appear(self.I_SA_CHECK_SELECT_SVR_2, False, 1)
            # self.ui_click(self.C_SA_SELECT_SVR_CHARACTER_LIST, self.I_SA_CHECK_SELECT_SVR_2, 1.5)
            self.screenshot() """

        self.O_SA_SELECT_SVR_CHARACTER_LIST.keyword = characterName
        lastBottomCharacterTexts = None
        empty_ocr_count = 0
        # 扫描随页面和列表到底状态收敛，不再设置固定扫描轮数。
        while True:
            self.screenshot()
            if self.appear_then_click(self.I_CANCEL_TOINVITE,interval=1.5):
                continue
            ocrRes = self.O_SA_SELECT_SVR_CHARACTER_LIST.detect_and_ocr(self.device.image)
            if not any(item.ocr_text.strip() for item in ocrRes):
                # 加载或动画可能短暂遮住文字，连续三帧为空才交账号级重试。
                empty_ocr_count += 1
                if empty_ocr_count >= CHARACTER_LIST_EMPTY_OCR_LIMIT:
                    logger.warning('Character list OCR empty for %d consecutive frames', empty_ocr_count)
                    break
                logger.info('Character list OCR empty (%d/%d), wait and retry in place',
                            empty_ocr_count, CHARACTER_LIST_EMPTY_OCR_LIMIT)
                time.sleep(CHARACTER_LIST_EMPTY_OCR_DELAY)
                continue
            # 有效帧结束本段连续空白；空帧不滑动，也不更新列表到底的判据。
            empty_ocr_count = 0
            bottomCharacterTexts = self._bottom_ocr_texts(ocrRes)
            ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
            # 本帧只识别一次列表，先查区服名，未命中再从同一份结果查角色名。
            index = find_character_index(ocrRes, characterName, svrName)
            if index is not None:
                tmp = self.O_SA_SELECT_SVR_CHARACTER_LIST
                from copy import deepcopy
                tmpClick = RuleClick(
                    roi_back=deepcopy(tmp.roi),
                    roi_front=[
                        tmp.roi[0] + ocrResBoxList[index][0][0],
                        tmp.roi[1] + ocrResBoxList[index][0][1],
                        ocrResBoxList[index][1][0] - ocrResBoxList[index][0][0],
                        ocrResBoxList[index][2][1] - ocrResBoxList[index][1][1]],
                    name="tmpClick"
                )
                #logger.info(tmpClick.roi_front)
                self.ui_click_until_disappear(tmpClick, stop=self.I_SA_CHECK_SELECT_SVR_1,
                                              interval=3)
                # 现场日志确认点击角色名即可返回登录页，下面的旧图标偏移示例保持停用。
                """ tmpClick.roi_front[1] -= 30
                self.ui_click_until_disappear(tmpClick, stop=self.I_SA_CHECK_SELECT_SVR_1,
                                              interval=3) """
                logger.info("character %s found,and clicked svr icon", characterName)
                return True
            if lastBottomCharacterTexts == bottomCharacterTexts:
                break
            logger.info(f'{characterName} not found,start swipe')
            lastBottomCharacterTexts = bottomCharacterTexts
            self.swipe(self.S_SA_SVR_SWIPE_LEFT)
            # 等待滑动动画完成
            time.sleep(1.5)

        self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT,stop=self.I_SA_CHECK_SELECT_SVR_1,interval=1.5)
        return False

    def jump2SelectAccount(self):
        """
            跳转到切换账号页面 该页面有红色登录按钮
        @return:
        @rtype:
        """
        while 1:
            if self.appear(self.I_SA_NETEASE_GAME_LOGO) and self.appear(self.I_SA_ACCOUNT_LOGIN_BTN):
                return
            if self.appear_then_click(self.I_SA_SWITCH_ACCOUNT_BTN, interval=1.5):
                continue
            if self.appear(self.I_CHECK_LOGIN_FORM):
                self.click(self.C_SA_LOGIN_FORM_USER_CENTER, 1.5)
                continue
        return

    def _ensure_game_alive(self):
        # 切换账号过程中，若检测到MuMu桌面壁纸，则判定游戏已闪退到桌面，抛异常交由恢复逻辑处理
        if self.appear(self.I_PAGE_DESKTOP):
            logger.warning("Detected MuMu desktop, game crashed to desktop while switching account")
            raise GameNotRunningError("Game crashed to desktop while switching account")

    def selectAccount(self, accountInfo: AccountInfo):
        logger.info("start selectAccount")
        self.O_SA_ACCOUNT_ACCOUNT_LIST.keyword = accountInfo.account
        self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
        account_list_swipe_start = None
        # 正常情况一次就行,但防住OCR搞幺子 多来几次保险起见 反正挂机不差这点
        for i in range(3):
            # 到底后先核验收起的表单，核验不符才消耗本轮扫描次数。
            account_list_finished = False
            awaiting_login_form = False
            while 1:
                self.screenshot()
                # 下拉展开才属于列表扫描阶段；重新展开后结束收起过渡的等待。
                account_list_opened = is_account_dropdown_opened(self)
                if account_list_opened:
                    awaiting_login_form = False
                # 优先检查是否已经出现登录按钮（即账号已选中）
                if self.appear(self.I_SA_ACCOUNT_LOGIN_BTN):
                    awaiting_login_form = False
                    # 验证选中的账号是否正确
                    # 直接用原始截图：_prepare_image_for_ocr 的 OTSU 二值化会把字符
                    # 抗锯齿边缘硬化，PP-OCRv6 失去区分 l / I / li 的灰度线索，
                    # 实测把 ljjiang7@ 读成 lijjiang7@ / Ijjiang7@ 导致验证永远不通过。
                    # 原始图分数更高（0.988 vs 0.94），无需预处理。
                    ocr_result = self.O_SA_ACCOUNT_ACCOUNT_SELECTED.detect_and_ocr(self.device.image)
                    if any(accountInfo.is_account_alias(ocr_item.ocr_text) for ocr_item in ocr_result):
                        logger.info("Account already selected and verified, login button appeared")
                        return True
                    else:
                        # 账号不对，尝试重新选择
                        logger.warning("Login button appeared but wrong account selected")
                        if account_list_finished:
                            break
                        if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED):
                            self.ui_click_until_disappear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED, interval=1.5)
                            continue
                        else:
                            return False  # 无法重新选择，返回失败

                # 主动选中或关闭列表后，等红按钮出现再按原邮箱别名规则核验。
                # CLOSED 可能先于红按钮出现，此时不能因严格整串 OCR 不符又重开列表。
                if not awaiting_login_form and self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED):
                    # 检查当前选中的账号是否是我们想要的
                    self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
                    if self.ocr_appear(self.O_SA_ACCOUNT_ACCOUNT_SELECTED):
                        logger.info(f"Confirmed account {accountInfo.account} is selected")
                        return True
                    if account_list_finished:
                        break
                    # 如果下拉框关闭但账号不对，重新打开下拉列表
                    self.ui_click_until_disappear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,
                                                  interval=1.5)
                    continue
                
                # 检查是否在苹果/安卓选择界面（表示账号选择已完成）
                if self.appear(self.I_SA_LOGIN_FORM_APPLE):
                    self.ui_click_until_disappear(self.I_SA_APPLE_BACK,stop=self.I_SA_LOGIN_FORM_APPLE, interval=1.5)
                    retry_count =0
                    while 1:
                        if retry_count > 3:
                            return False
                        self.screenshot()
                        if self.appear(self.I_SA_ACCOUNT_LOGIN_BTN):
                            break
                        self.click(self.C_SA_LOGIN_FORM_ENTER_GAME_BTN)
                        retry_count += 1
                        time.sleep(1.5)
                    continue

                # 未确认下拉展开时只等待页面转换，真实闪退仍交原设备恢复处理。
                if not account_list_opened:
                    self._ensure_game_alive()
                    continue

                # 账号列表已确认展开，才可识别列表里的账号名。
                # 同上：账号名是长英文串，二值化后 l / I 无法区分，直接用原始截图
                ocrRes = self.O_SA_ACCOUNT_ACCOUNT_LIST.detect_and_ocr(self.device.image)

                # 找到该账号
                for index, ocr_account in enumerate([ocrResItem.ocr_text for ocrResItem in ocrRes]):
                    if not accountInfo.is_account_alias(ocr_account):
                        continue
                    # 如果找到匹配的账号，创建点击对象
                    ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
                    
                    # 计算点击区域，使用OCR检测到的文字框的中心位置
                    roi_x = self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[0] + ocrResBoxList[index][0][0]
                    roi_y = self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[1] + ocrResBoxList[index][0][1]
                    roi_width = ocrResBoxList[index][1][0] - ocrResBoxList[index][0][0]
                    roi_height = ocrResBoxList[index][2][1] - ocrResBoxList[index][1][1]
                    
                    # 创建一个较小的点击区域，确保点击在文字中心附近
                    # 避免点击到相邻账号区域
                    click_roi = [
                        roi_x + roi_width * 0.2,  # 从左边20%处开始
                        roi_y + roi_height * 0.2, # 从上边20%处开始
                        roi_width * 0.6,          # 使用60%的宽度
                        roi_height * 0.6          # 使用60%的高度
                    ]
                    
                    account_click = RuleClick(
                        roi_back=click_roi,
                        roi_front=click_roi,
                        name="account_select"
                    )
                    
                    time.sleep(0.3)
                    logger.info("account [ %s ] found at position (%.1f, %.1f)", accountInfo.account, roi_x, roi_y)
                    
                    # 每次点击前确认列表仍展开，收起只是转入表单核验，不能直接报成功。
                    retry_count=0
                    while 1:
                        self.screenshot()
                        if (self.appear(self.I_SA_ACCOUNT_LOGIN_BTN)
                                or not is_account_dropdown_opened(self)):
                            awaiting_login_form = True
                            break
                        # 第五次点击后也先检查收起状态，仍展开才按原点击预算判失败。
                        if retry_count >= 5:
                            logger.warning("account [ %s ] clicked 5 times but dropdown is still open",
                                           accountInfo.account)
                            return False
                        self.click(account_click)
                        retry_count += 1
                        time.sleep(1.5)
                    time.sleep(1)  # 增加等待时间，确保界面响应
                    
                    # 不要立即截图验证，而是跳出内层循环，让外层循环进行状态检查
                    # 这样可以让代码自然地流转到上面的状态检查部分
                    break  # 跳出 for，回到 while 顶部做状态检查

                # for 正常结束（未 break）＝本屏 OCR 未找到账号才走这里。
                # 找到账号的路径绝不能再滑动：点击选中后下拉已收起，
                # S_SA_ACCOUNT_LIST_UP 的起点 (697,537,28,9) 会落在登录表单的
                # 「选择其他登录方式」热区上，滑动的 touch-down 直接触发页面跳转
                else:
                    # OCR 期间页面也可能已变化，滑动或点击收起按钮前重新确认下拉。
                    self.screenshot()
                    if not is_account_dropdown_opened(self):
                        awaiting_login_form = True
                        continue
                    # 列表到底后收起并核验，包括第三轮，不能直接跳过末次账号复查。
                    if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT):
                        retry_count =0
                        while 1:
                            self.screenshot()
                            if (self.appear(self.I_SA_ACCOUNT_LOGIN_BTN)
                                    or not is_account_dropdown_opened(self)):
                                awaiting_login_form = True
                                account_list_finished = True
                                break
                            # 保留原四次点击预算，最后一次生效后仍先检查页面转换。
                            if retry_count >= 4:
                                return False
                            self.click(self.C_SA_LOGIN_FORM_DROPDOWN_BTN)
                            retry_count += 1
                            time.sleep(1.5)
                        continue
                    if account_list_swipe_start is None:
                        account_list_swipe_start = time.time()
                    elif time.time() - account_list_swipe_start >= 60:
                        # 连续滑动60秒未收敛时，检测是否闪退到MuMu桌面，避免在桌面上持续滑动
                        self._ensure_game_alive()
                        account_list_swipe_start = time.time()
                    self.swipe(self.S_SA_ACCOUNT_LIST_UP, 1.5)
                    time.sleep(0.5)
                    continue  # 继续while循环，重新OCR识别
        logger.info("account [ %s ] not found after multiple attempts", accountInfo.account)
        return False
        """ def selectAccount(self, accountInfo: AccountInfo):
        logger.info("start selectAccount")
        self.O_SA_ACCOUNT_ACCOUNT_LIST.keyword = accountInfo.account
        self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
        # 正常情况一次就行,但防不住OCR搞幺蛾子 保险起见 多来几次吧 反正挂机不差这点
        for i in range(5):
            while 1:
                self.screenshot()
                if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED):
                    if self.ocr_appear(self.O_SA_ACCOUNT_ACCOUNT_SELECTED):
                        return True
                    self.ui_click_until_disappear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,
                                                  interval=1.5)
                    continue
                if self.appear(self.I_SA_LOGIN_FORM_APPLE):
                    return False

                # 账号列表已打开状态
                ocrRes = self.O_SA_ACCOUNT_ACCOUNT_LIST.detect_and_ocr(_prepare_image_for_ocr(self.device.image, asset=self.O_SA_ACCOUNT_ACCOUNT_LIST))
                
                # 找到该账号
                for index, ocr_account in enumerate([ocrResItem.ocr_text for ocrResItem in ocrRes]):
                    if not accountInfo.is_account_alias(ocr_account):
                        continue
                    # if accountInfo.account in [ocrResItem.ocr_text for ocrResItem in ocrRes]:
                    #     index = [ocrResItem.ocr_text for ocrResItem in ocrRes].index(accountInfo.account)
                    ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
                    
                    # 修正点击区域的计算方式，使用原始OCR检测区域的中心点，而不是缩放和偏移
                    roi_x = self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[0] + ocrResBoxList[index][0][0]
                    roi_y = self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[1] + ocrResBoxList[index][0][1]
                    roi_width = ocrResBoxList[index][1][0] - ocrResBoxList[index][0][0]
                    roi_height = ocrResBoxList[index][2][1] - ocrResBoxList[index][1][1]
                    
                    # 使用OCR区域的中心点作为点击位置，确保点击在文字区域中心
                    roi = [
                        roi_x + roi_width / 4,  # x坐标稍微往中心偏移
                        roi_y + roi_height / 4, # y坐标稍微往中心偏移
                        roi_width / 2,          # 宽度保持一半，但确保点击在中心
                        roi_height / 2          # 高度保持一半，但确保点击在中心
                    ]
                    self.O_SA_ACCOUNT_ACCOUNT_LIST.area = roi
                    acount_click = RuleClick(roi, roi, "account_select")
                    
                    
                    logger.info("account [ %s ] found", accountInfo.account)
                    self.click(acount_click)
                    time.sleep(1)
                # 未找到该账号
                if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT):
                    break
                self.swipe(self.S_SA_ACCOUNT_LIST_UP, 1.5)
                time.sleep(0.5)
        logger.info("account [ %s ] not found ", accountInfo.account)
        return False """

    # def loginSubmit(self, appleOrAndroid: bool):
    #     """
    #
    #     @param appleOrAndroid: 安卓平台还是苹果平台
    #     @type appleOrAndroid:   False           Apple
    #                             True            Android
    #     @return:
    #     @rtype:
    #     """
    #     self.screenshot()
    #     if not (self.appear(self.I_SA_ACCOUNT_LOGIN_BTN) and self.appear(self.I_SA_NETEASE_GAME_LOGO)):
    #         # 不在登录界面,返回失败
    #         return False
    #     self.ui_click(self.C_SA_LOGIN_FORM_LOGIN_BTN, self.I_SA_LOGIN_FORM_APPLE, 1)
    #     if appleOrAndroid:
    #         logger.info("APPLE selected")
    #         self.ui_click_until_disappear(self.I_SA_LOGIN_FORM_APPLE, 1)
    #     else:
    #         logger.info("ANDROID selected")
    #         self.ui_click_until_disappear(self.I_SA_LOGIN_FORM_ANDROID, 1)
    #     return True

    def login(self, accountInfo: AccountInfo) -> bool:
        """

        @param accountInfo:
        @type accountInfo:
        @return:    True    已核验账号并命中角色或区服，后续由 LoginHandler 进入游戏
                    False   配置无效或角色、区服均未命中
        @rtype:bool
        """
        # 直接调用登录组件也必须校验，不能只依赖任务队列过滤空白配置。
        if not accountInfo or not accountInfo.is_valid():
            logger.error("账号、角色名、区服名均必填，当前目标存在空项，中止登录")
            return False
        self.screenshot()
        #
        if not (self.appear(self.I_CHECK_LOGIN_FORM) or self.appear(self.I_SA_NETEASE_GAME_LOGO)):
            logger.error("Unknown Page,%s %s Login Failed", accountInfo.character, accountInfo.svr)
            return False

        #
        isAccountLogon = False
        isCharacterSelected = False
        self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
        self.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT.keyword = accountInfo.account
        while 1:
            self.screenshot()
            # 处于 选择服务器界面 直接点击空白区域退出该界面 进入切换账号流程
            if self.appear(self.I_SA_CHECK_SELECT_SVR_1):
                self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
                continue

            # 处于选择 苹果安卓界面
            if self.appear(self.I_SA_LOGIN_FORM_APPLE):
                btn = self.I_SA_LOGIN_FORM_ANDROID if accountInfo.apple_or_android else self.I_SA_LOGIN_FORM_APPLE
                self.ui_click_until_disappear(btn)
                time.sleep(1)
                # 保留本轮已完成的账号核验；起始就在平台页时仍为 False，随后补验用户中心。
                continue
            # 处于选择账号界面
            if self.appear(self.I_SA_NETEASE_GAME_LOGO) and not self.appear(self.I_SA_LOGIN_FORM_APPLE):
                if not accountInfo.account:
                    logger.error("param account is None,cannot switch account")
                    return False
                # 当前选择账号不是account
                if not self.ocr_appear(self.O_SA_ACCOUNT_ACCOUNT_SELECTED):
                    # 没有找到account
                    if not self.selectAccount(accountInfo):
                        # 最后一次点击可能刚生效；复查正确后继续登录和选角，不能提前报成功。
                        self.screenshot()
                        if not self.ocr_appear(self.O_SA_ACCOUNT_ACCOUNT_SELECTED):
                            if self.appear(self.I_SA_LOGIN_FORM_APPLE):
                                return False
                            self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ACCOUNT_CLOSE_BTN,
                                                          stop=self.I_SA_NETEASE_GAME_LOGO)
                            return False
                    # selectAccount 后更新图片
                    self.screenshot()
                self.ui_click(self.I_SA_ACCOUNT_LOGIN_BTN, stop=self.I_SA_LOGIN_FORM_APPLE, interval=3)
                # 目标账号已核验且登录提交已到平台页，不再重复打开用户中心读取邮箱。
                isAccountLogon = True
                continue
            # 在用户中心界面
            if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                # 如果当前已登录用户就是account
                ocrRes = self.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT.ocr_single(self.device.image)
                # 沿用原有邮箱预处理和别名规则，只读取既有账号区域，不扩大范围复读。
                if (accountInfo.account is None) or accountInfo.account == "" or accountInfo.is_account_alias(ocrRes):
                    logger.info("current is the account we want:ocr result %s", ocrRes)
                    isAccountLogon = True
                    self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_USER_CENTER_CLOSE_BTN, interval=1,
                                                  stop=self.I_SA_SWITCH_ACCOUNT_BTN)
                    continue
                #
                if self.ui_click(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO):
                    isAccountLogon = False
                    continue
                continue
            # 在游戏登录界面 不在用户中心 不在切换账号界面
            if not (self.appear(self.I_SA_NETEASE_GAME_LOGO) or self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)):
                # 判断是否已经账号登录
                if not isAccountLogon:
                    self.click(self.C_SA_LOGIN_FORM_USER_CENTER)
                    continue

                # 已登录 查找对应角色
                if not isCharacterSelected and self.switch_character(accountInfo.character, accountInfo.svr):
                    isCharacterSelected = True
                    continue
                break
            continue

        # 角色或区服已在同一帧内完成二选一匹配，不再另开列表重复 OCR。
        if isAccountLogon and isCharacterSelected:
            # 这里只完成账号核验和角色定位，进入庭院后才由 SwitchAccount 报登录成功。
            # self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ENTER_GAME_BTN, stop=self.I_CHECK_LOGIN_FORM)
            logger.info("character %s-%s account:%s %s ready for game login", accountInfo.character, accountInfo.svr,
                        accountInfo.account,
                        'Android' if accountInfo.apple_or_android else 'Apple')
            return True

        logger.error("character %s-%s account:%s %s login Failed", accountInfo.character, accountInfo.svr,
                     accountInfo.account,
                     'Android' if accountInfo.apple_or_android else 'Apple')
        return False

    def ui_click_until_disappear(self, click, interval: float = 1, stop: RuleImage | RuleGif = None):
        """
        重写原ui_click_until_disappear方法,增加stop参数
        点击一个按钮直到stop消失
        如果click为RuleOcr ,直接当作RuleClick点击,不会进行ocr识别,
        @param interval:
        @param click:
        @param stop:
        @type stop:
        @return:
        """
        if (isinstance(click, RuleImage) or isinstance(click, RuleGif)) and (stop is None):
            stop = click
        while 1:
            self.screenshot()
            if not self.appear(stop):
                break
            if isinstance(click, RuleImage) or isinstance(click, RuleGif):
                self.appear_then_click(click, interval=interval)
                continue
            elif isinstance(click, RuleClick):
                self.click(click, interval)
                continue
            elif isinstance(click, RuleOcr):
                self.click(click)
                continue
