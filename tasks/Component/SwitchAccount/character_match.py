"""共用角色列表的空帧等待规则；同一份 OCR 先查区服，再查角色。"""

# 两套列表都容许连续三帧空 OCR（含首次），前两帧原地等 0.5 秒再截图。
# 识别到有效文字就清零计数，不限制整个列表的扫描轮数。
CHARACTER_LIST_EMPTY_OCR_LIMIT = 3
CHARACTER_LIST_EMPTY_OCR_DELAY = 0.5


def normalize_name(text: str) -> str:
    """统一游戏内常见异体字，保留名字的其他字符。"""
    return text.replace('瑤', '瑶').replace('別', '别')


def is_character_name(ocr_text: str, character_name: str) -> bool:
    """匹配角色名，兼容左侧等级徽章粘连，明确拒绝空白目标。"""
    if not character_name or not character_name.strip():
        return False
    item = normalize_name(ocr_text)
    character_name = normalize_name(character_name)
    if not item.endswith(character_name):
        return False
    prefix = item[:-len(character_name)]
    # 只容许两位以内等级数字，以及等级与名字之间的一个空格。
    if prefix.endswith(' '):
        prefix = prefix[:-1]
    return len(prefix) <= 2 and (not prefix or prefix.isdigit())


def find_character_index(ocr_results, character_name: str = '', server_name: str = '', *,
                         legacy_either: bool = False) -> int | None:
    """返回区服名或角色名命中的原 OCR 下标；当前屏均未命中时返回 None。

    区服名在同一屏里唯一，所以先按区服精确定位，未命中才退回角色名后缀匹配；
    重复文字沿用 OCR 顺序选择第一个。
    legacy_either 保留 Restart 旧版「角色名/服务器名」单槽的区服用法。
    """
    character_name = character_name.strip()
    server_name = server_name.strip()
    # 先完整检查区服名，再复用本帧结果查角色名，不能额外触发第二次 OCR。
    svr_target = normalize_name(server_name or (character_name if legacy_either else ''))
    if svr_target:
        for index, item in enumerate(ocr_results):
            if normalize_name(item.ocr_text) == svr_target:
                return index
    if character_name:
        for index, item in enumerate(ocr_results):
            if is_character_name(item.ocr_text, character_name):
                return index
    return None
