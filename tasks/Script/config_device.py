# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from enum import Enum
from typing import Union
from pydantic import BaseModel, ValidationError, Field

from module.logger import logger
# 拟人化档位枚举唯一来源在 module.device.humanize，这里只导入不复制定义（Plan Task 12）
from module.device.humanize import HumanizeLevel

class PackageName(str, Enum):
    AUTO = 'auto'
    NETEASE_ONMYOJI = 'com.netease.onmyoji.wyzymnqsd_cps'  # 网易自家的阴阳师
    NETEASE_MI = 'com.netease.onmyoji.mi'  # 小米
    NETEASE = 'com.netease.onmyoji'
    NETEASE_HUAWEI = 'com.netease.onmyoji.huawei'
    NETEASE_BILIBILI = 'com.netease.onmyoji.bili'

class ScreenshotMethod(str, Enum):
    AUTO = 'auto'
    ADB = 'ADB'
    ADB_NC = 'ADB_nc'
    UIAUTOMATOR2 = 'uiautomator2'
    DROIDCAST = 'DroidCast'
    DROIDCAST_RAW = 'DroidCast_raw'
    SCRCPY = 'scrcpy'
    WINDOW_BACKGROUND = 'window_background'
    PRINTWINDOW = 'printwindow'
    NEMU_IPC = 'nemu_ipc'

class ControlMethod(str, Enum):
    ADB = 'adb'
    UIAUTOMATOR2 = 'uiautomator2'
    MINITOUCH = 'minitouch'
    # MuMu 官方 IPC 注入：内核级事件、设备侧零驻留进程（仅 MuMu 实例可用）
    NEMU_IPC = 'nemu_ipc'
    WINDOW_MESSAGE = 'window_message'

class EmulatorInfoType(str, Enum):
    # module.device.platform2.emulator_base.EmulatorBase
    AUTO = 'auto'
    NoxPlayer = 'NoxPlayer'
    NoxPlayer64 = 'NoxPlayer64'
    BlueStacks4 = 'BlueStacks4'
    BlueStacks5 = 'BlueStacks5'
    BlueStacks4HyperV = 'BlueStacks4HyperV'
    BlueStacks5HyperV = 'BlueStacks5HyperV'
    LDPlayer3 = 'LDPlayer3'
    LDPlayer4 = 'LDPlayer4'
    LDPlayer9 = 'LDPlayer9'
    MuMuPlayer = 'MuMuPlayer'
    MuMuPlayerX = 'MuMuPlayerX'
    MuMuPlayer12 = 'MuMuPlayer12'
    MEmuPlayer = 'MEmuPlayer'

class Device(BaseModel):
    serial: str = Field(default="auto",
                        description='serial_help')
    handle: str = Field(default='',
                        description='handle_help')
    # 桌面客户端自动启动用：游戏安装目录（含 bin\onmyoji.exe 或 Launch.exe 的根目录），
    # 留空时按 %ProgramFiles%\Onmyoji 与注册表 InstallLocation 自动发现；仅桌面模式使用
    desktop_game_path: str = Field(default='',
                                   description='桌面客户端游戏安装目录，留空自动发现（仅桌面模式）')
    package_name: PackageName = Field(title='Package Name',
                                      default=PackageName.AUTO,
                                      description='package_name_help')
    screenshot_method: ScreenshotMethod = Field(default=ScreenshotMethod.AUTO,
                                                description='screenshot_method_help')
    control_method: ControlMethod = Field(default=ControlMethod.MINITOUCH,
                                          description='control_method_help')
    adb_restart: bool = Field(default=False,
                              description='adb_restart_help')
    emulatorinfo_type: Union[EmulatorInfoType, str] = Field(default=EmulatorInfoType.AUTO,
                                                description='emulatorinfo_type_help')
    emulatorinfo_name: str = Field(default='',
                                   description='emulatorinfo_name_help')
    emulatorinfo_path: str = Field(default='',
                                   description='emulatorinfo_path_help')
    # 举例, E:\ProgramFiles\MuMuPlayer-12.0\shell\MuMuPlayer.exe
    # 模拟器启动时最小化
    emulator_window_minimize: bool = Field(default=False,
                                             description='模拟器静默启动并最小化')
    # 启动时纯后台运行模拟器，不显示窗口和任务栏
    run_background_only: bool = Field(default=False,
                                             description='模拟器无UI后台运行，关掉后重启脚本会重新显示（无需重启OAS）')
    # 拟人化输入档位。默认 off 是零回归旁路（事件/时长/随机序列逐字节不变）；
    # medium 开启落点采样、按压时长、几何轨迹、速度剖面、到位停顿与动作间隔，
    # Python 逐点 sleep 有毫秒级时间代价。
    # 合法取值固定为 off/medium（原 light/heavy 已于 2026-09-13 收敛删除，
    # 旧值由 config_validation 的 legacy 迁移改写成 medium），
    # 定义唯一来源在 module.device.humanize。
    humanize_level: HumanizeLevel = Field(default='off',
                                          description='拟人化输入档位：off 零回归全旁路；medium 开启落点/按压/轨迹/停顿/动作间隔拟人（逐点 sleep 约几十毫秒时间代价）')



if __name__ == '__main__':
    d = Device()
    print(d.json())
    print(d.schema_json())
    try:
        d.control_method = 'adb'
    except ValidationError as e:
        print(e)
