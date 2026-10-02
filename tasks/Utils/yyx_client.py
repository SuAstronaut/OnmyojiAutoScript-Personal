"""在游戏 Python VM 中采集 YYX 八组位置数组；宿主导入本模块不会访问游戏。"""


def collect_snapshot():
    """保留原字段顺序，扩展寄售券与传记归属；关键接口或配置缺失时直接抛出异常。"""
    # 本文件由宿主读取后在游戏中 exec，游戏模块必须延迟到真正采集时导入。
    import Globals
    from DynamicConfigData import DATA_HERO, DATA_EQUIP_RANDOM_ATTR, DATA_STORY
    import com.utils.helpers as helpers
    import com.const as CONST

    player = Globals.player1
    if player is None:
        raise RuntimeError('Globals.player1: 角色数据尚未加载')

    hero_types = (
        CONST.HeroType.SS_MONSTER,
        CONST.HeroType.SS_GHOST,
        CONST.HeroType.SS_ELF,
    )
    # 序号与 YYX EquipAttrType 枚举一致；未知属性必须报错，不能丢弃。
    equip_attr_types = (
        'maxHpAdditionVal', 'defenseAdditionVal', 'attackAdditionVal',
        'maxHpAdditionRate', 'defenseAdditionRate', 'attackAdditionRate',
        'speedAdditionVal', 'critRateAdditionVal', 'critPowerAdditionVal',
        'debuffEnhance', 'debuffResist',
    )
    equip_attr_type_map = {
        name: index for index, name in enumerate(equip_attr_types)
    }

    def map_equip(equip_id, equip):
        """御魂 API 失败必须中止整份快照，不用零值冒充等级、位置或属性。"""
        attrs = [
            [equip_attr_type_map[name], value]
            for name, value in equip.getRandAttrDict().items()
        ]
        base_attr = [
            equip_attr_type_map[equip.baseAttrName],
            equip.strengthenedBaseAttrValue,
        ]
        single_attrs = []
        # 无首领御魂固定属性是正常情况；有属性编号时必须找到完整配置。
        if equip.single_attr:
            single_attrs = [
                [equip_attr_type_map[attr[0]], attr[1]]
                for attr in DATA_EQUIP_RANDOM_ATTR.data[equip.single_attr]['attrs']
            ]
        return [
            equip_id,
            equip.suitId,
            int(equip.getEquipInit('quality')),
            int(equip.getPos()),
            equip.equipId,
            equip.strongLevel,
            equip.born,
            bool(equip.lock),
            bool(equip.garbage),
            attrs,
            base_attr,
            # attrs 与 random_attrs 沿用原采集器的 getter；强化比例直接读取
            # 实机确认的 randAttrRates，缺字段或未知属性必须让整份快照失败。
            attrs,
            [[equip_attr_type_map[name], rate]
             for name, rate in equip.randAttrRates.items()],
            single_attrs,
        ]

    def map_hero(hero_id, hero):
        """觉醒状态决定属性 API；每个属性均直接读取，缺字段不再默认成零。"""
        attr = (hero.getUnAwakeBattleAttr()
                if hero.awake == 0 else hero.getAwakeBattleAttr())
        equips = hero._equips
        # 游戏用 None 表示未装备御魂，但 _equips 接口本身缺失仍需报错。
        if equips is None:
            equips = []
        return [
            hero_id,
            hero.heroId,
            equips,
            hero._level,
            hero.exp,
            hero.nickName,
            hero.born,
            bool(hero.lock),
            hero.rarity,
            [[skill[0], skill[1]] for skill in hero.skillList],
            hero.awake,
            hero.star,
            [
                [attr.baseMaxHp, attr.maxHpAdditionVal, attr.maxHpAdditionRate, attr.maxHp],
                [attr.baseSpeed, attr.speedAdditionVal, attr.speedAdditionRate, attr.speed],
                [attr.baseCritPower, attr.critPowerAdditionVal, attr.critPowerAdditionRate, attr.critPower],
                [attr.baseCritRate, attr.critRateAdditionVal, attr.critRateAdditionRate, attr.critRate],
                [attr.baseDefense, attr.defenseAdditionVal, attr.defenseAdditionRate, attr.defense],
                [attr.baseAttack, attr.attackAdditionVal, attr.attackAdditionRate, attr.attack],
                attr.debuffEnhance,
                attr.debuffResist,
            ],
        ]

    def get_item_presets():
        """未保存预设允许为空；读取失败或名称缺项不能当作没有预设。"""
        preset_items = helpers.getUserConfig('equipDrawer', [])
        preset_names = helpers.getUserConfig('equipDrawerName', [])
        if len(preset_names) < len(preset_items):
            raise ValueError('equipDrawerName: 御魂预设缺少名称')
        return [[preset_names[index], items]
                for index, items in enumerate(preset_items)]

    def get_hero_shards():
        """只导出原脚本支持的式神类型，未持有的货币数量按游戏约定为零。"""
        items = []
        for hero_id, data in DATA_HERO.data.items():
            if data['type'] not in hero_types:
                continue
            book = data['book']
            items.append([
                hero_id,
                player.currency.get(book[1], 0),
                player.currency.get(book[0], 0),
                book[2],
            ])
        return items

    def get_story_tasks():
        """按式神类型采集全部传记；传记可不存在，已有任务的进度读取必须成功。"""
        items = []
        for hero_id, data in DATA_HERO.data.items():
            # 与本体、碎片使用相同范围，避免石长姬及后续新编号被上限过滤。
            if data['type'] not in hero_types:
                continue
            story_data = DATA_STORY.data.get(hero_id)
            if story_data is None:
                continue
            activity_ids = story_data.get('activityId')
            if activity_ids is not None:
                # 章节取游戏配置原顺序；同一任务属于多位式神时逐条保留归属。
                for chapter, activity_id in enumerate(activity_ids, start=1):
                    items.append([
                        activity_id, Globals.jobMgr.getJobProg(activity_id),
                        hero_id, chapter,
                    ])
        return items

    # 前 21 个位置与 YYX Currency 一致，末尾追加实机核实的寄售券，避免原字段错位。
    currency_ids = [
        CONST.CurrencyType.COIN,      # coin 金币
        CONST.CurrencyType.GOLD,      # jade 勾玉
        CONST.CurrencyType.STRENGTH,  # action_point 体力
        900273,                      # auto_point 樱饼
        900012,                      # honor 荣誉
        900016,                      # medal 勋章
        900090,                      # contrib 功勋
        900215,                      # totem_pass 御灵境之钥
        900000,                      # s_jade 魂玉
        900023,                      # skin_token 皮肤券
        900024,                      # realm_raid_pass 突破券
        490002,                      # broken_amulet 破碎符
        490001,                      # mystery_amulet 蓝票
        490004,                      # ar_amulet 现世符
        900178,                      # ofuda 御札
        900188,                      # gold_ofuda 金御札
        900216,                      # scale 八岐大蛇鳞片
        900217,                      # reverse_scale 逆鳞
        900218,                      # demon_soul 逢魔之魂
        900041,                      # foolery_pass 痴念之卷
        906058,                      # sp_skin_token SP 皮肤券
        900338,                      # consignment_ticket 寄售券（用于寄售屋购买，非 900339 勾玉券）
    ]
    # 已拥有的式神也必须找到类型配置，防止配置缺失时被过滤成“没有式神”。
    return [
        [player.short_id, player.server_id, player.name, player.level],
        [int(player.currency.get(currency_id, 0)) for currency_id in currency_ids],
        [map_hero(hero_id, hero) for hero_id, hero in player.heroes.items()
         if DATA_HERO.data[hero.heroId]['type'] in hero_types],
        [map_equip(equip_id, equip) for equip_id, equip in player.inventory.items()],
        get_item_presets(),
        get_hero_shards(),
        [[card_id, card.itemid, card.totalTime, card.produceValue]
         for card_id, card in player.myJiejieCardDataDict.items()],
        get_story_tasks(),
    ]
