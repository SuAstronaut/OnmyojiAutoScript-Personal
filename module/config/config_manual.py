# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from tasks.Utils.optional_tasks import extend


class ConfigManual:
    """
    module.device
    """

    # 账号导出与多账号通用任务同组，独立启用时按自己的调度执行。
    SCHEDULER_PRIORITY = """
        Restart > SoulsTidy
        > KekkaiUtilize > KekkaiActivation > MultiDailyAltAcc > MultiTasks > AccountExport > DailyAltAcc > MasterDisciple > Dokan > ReturnGift > DemonEncounter
        > AreaBoss > GoldYoukai > ExperienceYoukai > Nian > Tako > AutoCheckinBigGod > ActivitySignIn > RealmRaid > RyouToppa > DailyTrifles > Exploration >FindJade
        > AbyssShadows > Hunt > GuildBanquet > DemonRetreat > GuildActivityMonitor
        > Orochi > OrochiMoans > OrochiJudgement > Sougenbi > FallenSun > EternitySea > SixRealms
        > ActivityShikigami > WantedQuests
        > BondlingFairyland > EvoZone > GoryouRealm > HeroTest
        > CollectiveMissionsr
        > Pets > TalismanPass > Delegation > Hyakkiyakou
        > Secret > WeeklyTrifles > MysteryShop > Duel 
        > TrueOrochi > RichMan 
        > MetaDemon > FrogBoss > FloatParade > Quiz > KittyShop > DyeTrials > MemoryScrolls> Plotline >SearchId
        """

    # 调度只接收当前机器提供的附加优先级，缺少扩展时保留默认顺序。
    SCHEDULER_PRIORITY = extend('priority', SCHEDULER_PRIORITY)

    DEVICE_OVER_HTTP = False
    FORWARD_PORT_RANGE = (20000, 21000)
    REVERSE_SERVER_PORT = 7903

    # ASCREENCAP_FILEPATH_LOCAL = './bin/ascreencap'
    # ASCREENCAP_FILEPATH_REMOTE = '/data/local/tmp/ascreencap'

    # 'DroidCast', 'DroidCast_raw'
    DROIDCAST_VERSION = 'DroidCast'
    DROIDCAST_FILEPATH_LOCAL = './bin/droidcast/DroidCast_raw-release-1.0.apk'
    DROIDCAST_FILEPATH_REMOTE = '/data/local/tmp/DroidCast_raw.apk'

    MINITOUCH_FILEPATH_REMOTE = '/data/local/tmp/minitouch'

    HERMIT_FILEPATH_LOCAL = './bin/hermit/hermit.apk'
