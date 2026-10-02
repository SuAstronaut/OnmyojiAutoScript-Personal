import unicodedata

from pydantic import Field, BaseModel, field_validator

from tasks.Component.config_base import DateTime, ConfigBase
from module.logger import logger


def clean_text(value):
    """剔除不可见字符并去掉首尾空白。

    配置值来自 GUI 粘贴，常混入 \\r\\n 这类控制字符或零宽字符（BOM 等）。它们看不见，
    却会让字符串比较失败，也会让 acc_key 生成两个不同的进度条目、把 [STAT] 日志写脏。
    只删 Cc（控制）与 Cf（格式）两类不可见字符，名字中间的普通空格保留。
    """
    if not isinstance(value, str):
        return value
    return ''.join(
        ch for ch in value if unicodedata.category(ch) not in ('Cc', 'Cf')
    ).strip()


class AccountInfo(BaseModel):
    """
        character:   角色名字
        svr:         角色所在服务器
        account:     账号
        appleOrAndroid:  角色所属平台 安卓/苹果(可选)
                False           Apple
                True            Android
    """
    character: str = Field(default="", description='character_help')
    svr: str = Field(default="", description="svr_help")
    account: str = Field(default="", description="account_help")
    # 为防止ocr出错 暂定格式 字符串以#分割
    account_alias: str = Field(default="", description="account_alias_help")
    apple_or_android: bool = Field(default=True, description="apple_or_android_help")
    # 最近确认的登录时间由各任务共享，用于展示与切号排序；任务完成状态另行记录。
    last_complete_time: DateTime = Field(default=DateTime.fromisoformat("2023-01-01 00:00:00"), description="last_complete_time_help")

    @field_validator('character', 'svr', 'account', 'account_alias', mode='before')
    @classmethod
    def clean_text_fields(cls, v):
        """四个文本字段构造时统一清洗，保存时随之写回干净值。"""
        return clean_text(v)

    def is_account_alias(self, ocr_account):
        tmp_account = AccountInfo.preprocessAccount(self.account)
        ocr_account = ocr_account.lower()
        #logger.info(f"compare ocr_account:{ocr_account}  tmp_account:{tmp_account} ")
        if ocr_account == self.account or ocr_account.startswith(tmp_account):
            return True
        if not self.account_alias:
            return False
        _accountAliasList = self.account_alias.split('#')
        for alias in _accountAliasList:
            if '@' in self.account:
                alias =alias+'@'
            if ocr_account.startswith(alias):
                return True
        return False

    @staticmethod
    def preprocessAccount(account: str):
        """
            预处理账号信息 便于比对
            邮箱账号        保留@符号及@之前的部分，并全部转换为小写
        @param account:
        @type account:
        @return:
        @rtype:
        """

        if '@' in account:
            # 如果包含@符号，则只保留@及其前面的部分并转为小写
            return account.split('@')[0].lower() + '@'
        else:
            # 如果不包含@符号，则返回原账号的小写形式
            return account.lower()

    def is_valid(self):
        # 角色名、区服名、账号名全部必填；任一项为空或纯空白都必须跳过。
        return all(value and value.strip() for value in (self.character, self.svr, self.account))


class SwitchAccountConfig(ConfigBase):
    # 是否在任务开始前自动切换到目标账号
    enable: bool = Field(default=False, description='switch_account_enable_help')
