# This Python file uses the following encoding: utf-8
"""协作汇总推送的正文构造。

从 `tasks/MultiDailyAltAcc/script_task.py` 抽出，供需要发送协作通知的任务共用同一份
正文实现，保持协作分类、角色信息与数量的展示格式一致。

记录字段沿用既有协作结构——type / real /
food_kind / character / svr / account / apple_or_android / monster_text。
格式化只读取这些字段，不修改识别得到的奖励类型或现世标记。
"""
from datetime import datetime


def coop_category_order():
    """沿用 cd10fe10 的七类展示顺序：
    现世勾协/现世体协/普通勾协/普通体协/狗粮/猫粮/金币。

    只有勾协、体协按现世标记分组；粮协和金币合并展示，但原记录的 real 保持不变。
    旧记录没有 real 字段时，勾协、体协仍按普通类别展示。
    """
    return [
        ("现世勾协", lambda r: r.get("type") == "jade" and bool(r.get("real"))),
        ("现世体协", lambda r: r.get("type") == "sushi" and bool(r.get("real"))),
        ("普通勾协", lambda r: r.get("type") == "jade" and not bool(r.get("real"))),
        ("普通体协", lambda r: r.get("type") == "sushi" and not bool(r.get("real"))),
        ("狗粮协作", lambda r: r.get("type") == "food"
            and r.get("food_kind") == "dog"),
        ("猫粮协作", lambda r: r.get("type") == "food"
            and r.get("food_kind") == "cat"),
        ("金币协作", lambda r: r.get("type") == "gold"),
    ]


def build_mshop_lines(mshops, show_account=False, show_system=True) -> list:
    """格式化神秘商店段落；mshops 为空返回空列表（不产生空段落）。

    商店命中稀有，所以不像协作那样按角色聚合计数 —— 每一件单独一行列出
    货名与价格，方便直接判断值不值得手动去买。
    """
    if not mshops:
        return []
    lines = ["", f"神秘商店（{len(mshops)}）"]
    for rec in mshops:
        char = (rec.get("character") or "").strip() or "未知角色"
        line = f"• {char}"
        meta = []
        if rec.get("svr"):
            meta.append(str(rec["svr"]))
        platform = rec.get("apple_or_android")
        if show_system and platform is not None:
            meta.append("安卓" if platform else "iOS")
        if show_account and rec.get("account"):
            meta.append(str(rec["account"]))
        if meta:
            line += f"（{'｜'.join(meta)}）"
        # 有结构化货名/价格就拼「货名 价格币种」，否则回退到旧格式的整串 label
        goods = (rec.get("goods") or "").strip()
        price = rec.get("price")
        coin = (rec.get("coin") or "").strip()
        if goods and price is not None:
            line += f" {goods} {price}{coin}"
        elif rec.get("label"):
            line += f" {rec['label']}"
        lines.append(line)
    return lines


def build_summary_content(coops, completed_at=None, show_account=False,
                          show_system=True, mshops=None,
                          title="多账号日常完成") -> str:
    """按固定七类顺序格式化协作汇总文本，并在末尾追加神秘商店段落。

    title 是正文首行，由调用方给（不同任务各有自己的标识，其余
    部分两处逐字一致）；空协作空商店时它同样出现在提示正文里。

    show_system=True 时显示平台（安卓/iOS，取自 apple_or_android 字段）；
    show_account=True 时在角色行尾追加账号/邮箱（account 原值）。
    svr/account/platform 任一为空都不产生空分隔符。

    mshops 为空时完全不出商店段落（保持原有输出逐字不变）；协作为空但商店
    非空时，头部计数仍显示协作 0，末尾出商店段落 —— 不能因为没协作就把
    商店命中吞掉。
    """
    now_str = (completed_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    mshops = mshops or []
    if not coops and not mshops:
        return "\n".join([
            title,
            "",
            f"完成时间：{now_str}",
            "发现协作角色：0",
            "协作任务数量：0",
            "",
            "本轮未发现协作任务。",
        ])
    roles = set()
    for r in coops:
        char = (r.get("character") or "").strip()
        if char:
            roles.add((char, r.get("svr") or ""))
    lines = [
        title,
        "",
        f"完成时间：{now_str}",
        f"发现协作角色：{len(roles)}",
        f"协作任务数量：{len(coops)}",
    ]
    for category, matcher in coop_category_order():
        items = [r for r in coops if matcher(r)]
        if not items:
            continue
        counter = {}
        first_rec = {}
        for r in items:
            char = (r.get("character") or "").strip()
            if not char:
                continue
            monster_text = ""
            if (
                (r.get("type") == "jade" and not bool(r.get("real")))
                or r.get("type") == "sushi"
            ):
                monster_text = (r.get("monster_text") or "").strip()
            key = (char, r.get("svr") or "", monster_text)
            counter[key] = counter.get(key, 0) + 1
            first_rec.setdefault(key, r)
        lines.append("")
        lines.append(f"{category}（{len(items)}）")
        for (char, svr, monster_text), count in sorted(counter.items()):
            rec = first_rec[(char, svr, monster_text)]
            role_line = f"• {monster_text}：{char}" if monster_text else f"• {char}"
            meta = []
            if svr:
                meta.append(svr)
            # 平台：True=安卓，False=iOS；show_system 关闭或旧记录无该字段则不显示
            platform = rec.get("apple_or_android")
            if show_system and platform is not None:
                meta.append("安卓" if platform else "iOS")
            # 账号/邮箱：可选开关，开启后直接显示 account 原值
            if show_account and rec.get("account"):
                meta.append(str(rec["account"]))
            if meta:
                role_line += f"（{'｜'.join(meta)}）"
            if count > 1:
                role_line += f" ×{count}"
            lines.append(role_line)
    lines.extend(build_mshop_lines(
        mshops, show_account=show_account, show_system=show_system))
    return "\n".join(lines)
