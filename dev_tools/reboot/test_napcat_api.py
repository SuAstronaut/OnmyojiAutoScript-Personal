"""
测试 NapCat QQ群消息接口 - 模拟 OAS Dokan 道馆触发逻辑
用法: python test_napcat_api.py
"""
import requests
import json
import sys
import io
import re
from datetime import datetime

# 修复Windows终端编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ===== 配置 =====
# 按你自己机器的 NapCat 部署填写；令牌与群号属于私密信息，不要提交进仓库
ENDPOINT = "http://127.0.0.1:3000"
ACCESS_TOKEN = ""         # NapCat 的 access_token，留空则不发送 Authorization 头
GROUP_ID = 0              # 目标 QQ 群号
CREATE_SENDER_ID = 0      # 道馆创建关键词发送者QQ, 0=不限制
AT_ALL_SENDER_ID = 0      # @全体成员发送者QQ, 0=不限制(默认跟随CREATE_SENDER_ID)
# ================

headers = {'Content-Type': 'application/json'}
if ACCESS_TOKEN:
    headers['Authorization'] = f'Bearer {ACCESS_TOKEN}'


def get_group_messages(group_id, message_seq=0, count=20):
    """获取群消息历史"""
    url = f"{ENDPOINT}/get_group_msg_history"
    body = {'group_id': group_id, 'message_seq': message_seq, 'count': count}
    resp = requests.post(url, json=body, headers=headers, timeout=10)
    if resp.status_code != 200:
        print(f"[FAIL] 请求失败，状态码: {resp.status_code}")
        return None
    data = resp.json()
    if data.get('status') != 'ok':
        print(f"[FAIL] API返回错误: {data.get('wording', data)}")
        return None
    return data.get('data', {}).get('messages', [])


def parse_message(msg):
    """解析单条消息"""
    msg_id = msg.get('message_id', 0)
    msg_time = msg.get('time', 0)
    sender = msg.get('sender', {})
    sender_id = sender.get('user_id', 0)
    sender_name = sender.get('nickname', '')
    message_content = msg.get('message', '')

    has_at_all = False
    text_content = ''

    if isinstance(message_content, list):
        for seg in message_content:
            if isinstance(seg, dict):
                if seg.get('type') == 'at' and seg.get('data', {}).get('qq') == 'all':
                    has_at_all = True
                elif seg.get('type') == 'text':
                    text_content += seg.get('data', {}).get('text', '')
    elif isinstance(message_content, str):
        if '[CQ:at,qq=all]' in message_content:
            has_at_all = True
        text_content = re.sub(r'\[CQ:[^\]]+\]', '', message_content)

    return {
        'msg_id': msg_id,
        'msg_seq': msg.get('message_seq', 0),
        'time': msg_time,
        'time_str': datetime.fromtimestamp(msg_time).strftime('%Y-%m-%d %H:%M:%S') if msg_time else '',
        'sender_id': sender_id,
        'sender_name': sender_name,
        'text': text_content.strip(),
        'at_all': has_at_all,
    }


def test_api():
    """测试API连通性"""
    print("=" * 60)
    print("1. 测试 NapCat 连通性 (get_login_info)")
    print("=" * 60)
    url = f"{ENDPOINT}/get_login_info"
    try:
        resp = requests.post(url, json={}, headers=headers, timeout=5)
        data = resp.json()
        if data.get('status') == 'ok':
            info = data.get('data', {})
            print(f"[OK] NapCat 在线! QQ: {info.get('user_id')}, 昵称: {info.get('nickname')}")
        else:
            print(f"[FAIL] API返回: {data}")
            return False
    except Exception as e:
        print(f"[FAIL] 无法连接: {e}")
        return False
    return True


def simulate_trigger(filtered, create_keyword, create_sender_id, at_all_sender_id, require_at_all):
    """
    模拟OAS道馆触发逻辑 (与script_task.py中check_qq_group_message一致)
    """
    create_keyword_found = False
    at_all_found = False
    create_keyword_sender = 0
    at_all_sender = 0
    details = []

    for m in filtered:
        # 检查道馆创建关键词 + 发送者
        if create_keyword and create_keyword in m['text']:
            sender_match = (create_sender_id == 0 or m['sender_id'] == create_sender_id)
            if sender_match:
                create_keyword_found = True
                create_keyword_sender = m['sender_id']
                details.append(f"  [MATCH] 关键词 '{create_keyword}' 由 QQ:{m['sender_id']} 发送")
            else:
                details.append(f"  [SKIP] 关键词匹配但发送者不符 (期望:{create_sender_id}, 实际:{m['sender_id']})")

        # 检查@全体成员 + 发送者
        if m['at_all']:
            at_sender = at_all_sender_id if at_all_sender_id != 0 else create_sender_id
            sender_match = (at_sender == 0 or m['sender_id'] == at_sender)
            if sender_match:
                at_all_found = True
                at_all_sender = m['sender_id']
                details.append(f"  [MATCH] @全体成员 由 QQ:{m['sender_id']} 发送")
            else:
                details.append(f"  [SKIP] @全体成员但发送者不符 (期望:{at_sender}, 实际:{m['sender_id']})")

    if require_at_all:
        result = create_keyword_found and at_all_found
    else:
        result = create_keyword_found

    return result, create_keyword_found, at_all_found, create_keyword_sender, at_all_sender, details


def test_messages():
    """测试获取群消息并模拟道馆触发逻辑"""
    print()
    print("=" * 60)
    print("2. 获取群消息")
    print("=" * 60)

    now = datetime.now()
    today_9pm = datetime.combine(now.date(), datetime.min.time().replace(hour=21))
    nine_pm_timestamp = today_9pm.timestamp()

    # 如果当前还没到21点，临时用0点作为起点方便测试
    test_from_zero = False
    if now < today_9pm:
        today_0am = datetime.combine(now.date(), datetime.min.time())
        nine_pm_timestamp = today_0am.timestamp()
        test_from_zero = True

    all_messages = []
    message_seq = 0
    page = 0

    while page < 10:
        msgs = get_group_messages(GROUP_ID, message_seq=message_seq, count=20)
        if not msgs:
            break

        all_messages.extend(msgs)
        page += 1

        oldest_time = min(msg.get('time', 0) for msg in msgs)
        if oldest_time < nine_pm_timestamp:
            break

        earliest_seq = min(msg.get('message_seq', 0) for msg in msgs)
        if earliest_seq <= 0 or (earliest_seq >= message_seq and message_seq != 0):
            break
        message_seq = earliest_seq

    # 按 message_id 去重（NapCat翻页时边界消息可能重复返回）
    seen_ids = set()
    unique_messages = []
    for msg in all_messages:
        mid = msg.get('message_id')
        if mid not in seen_ids:
            seen_ids.add(mid)
            unique_messages.append(msg)
    all_messages = unique_messages

    print(f"共获取 {len(all_messages)} 条消息 (翻页{page}次)")

    # 筛选并解析
    filtered = []
    for msg in all_messages:
        if msg.get('time', 0) >= nine_pm_timestamp:
            filtered.append(parse_message(msg))

    filtered.sort(key=lambda x: x['time'])

    if not filtered:
        print("[WARN] 时间范围内暂无消息")
        return filtered

    print(f"时间范围内共 {len(filtered)} 条消息:")
    print("-" * 60)
    for m in filtered:
        at_mark = " [@全体] " if m['at_all'] else ""
        print(f"  [{m['time_str']}] {m['sender_name']}({m['sender_id']}){at_mark}: {m['text'][:80]}")

    return filtered


def test_sender_scenarios(filtered):
    """
    测试不同发送者配置下的触发结果
    与OAS script_task.py中check_qq_group_message逻辑完全一致
    """
    print()
    print("=" * 60)
    print("3. 发送者过滤测试 (多场景)")
    print("=" * 60)

    if not filtered:
        print("[WARN] 无消息，跳过测试")
        return

    # 找到实际发送者
    senders = set(m['sender_id'] for m in filtered)
    keyword_sender = None
    at_all_sender = None
    for m in filtered:
        if '道馆已经创建' in m['text'] or '道馆' in m['text']:
            keyword_sender = m['sender_id']
        if m['at_all']:
            at_all_sender = m['sender_id']

    print(f"  群内发送者: {senders}")
    print(f"  关键词发送者: {keyword_sender}")
    print(f"  @全体发送者: {at_all_sender}")
    print()

    # 构造测试场景
    scenarios = [
        {
            'name': f'场景0: 当前配置 (create_sender_id={CREATE_SENDER_ID}, at_all_sender_id={AT_ALL_SENDER_ID})',
            'create_keyword': '道馆已经创建',
            'create_sender_id': CREATE_SENDER_ID,
            'at_all_sender_id': AT_ALL_SENDER_ID,
            'require_at_all': True,
        },
        {
            'name': '场景1: 不限制发送者 (create_sender_id=0)',
            'create_keyword': '道馆已经创建',
            'create_sender_id': 0,
            'at_all_sender_id': 0,
            'require_at_all': True,
        },
        {
            'name': f'场景2: 限制关键词发送者为实际发送者 ({keyword_sender})',
            'create_keyword': '道馆已经创建',
            'create_sender_id': keyword_sender or 0,
            'at_all_sender_id': 0,
            'require_at_all': True,
        },
        {
            'name': '场景3: 限制关键词发送者为不存在的QQ (999999999)',
            'create_keyword': '道馆已经创建',
            'create_sender_id': 999999999,
            'at_all_sender_id': 0,
            'require_at_all': True,
        },
        {
            'name': '场景4: 限制@全体发送者为实际发送者',
            'create_keyword': '道馆已经创建',
            'create_sender_id': 0,
            'at_all_sender_id': at_all_sender or 999999999,
            'require_at_all': True,
        },
        {
            'name': '场景5: 不要求@全体成员',
            'create_keyword': '道馆已经创建',
            'create_sender_id': 0,
            'at_all_sender_id': 0,
            'require_at_all': False,
        },
    ]

    for s in scenarios:
        print(f"--- {s['name']} ---")
        result, kw_found, at_found, kw_sender, at_sender, details = simulate_trigger(
            filtered,
            s['create_keyword'],
            s['create_sender_id'],
            s['at_all_sender_id'],
            s['require_at_all'],
        )
        for d in details:
            print(d)
        kw_str = f"[OK] QQ:{kw_sender}" if kw_found else "[--] 未匹配"
        at_str = f"[OK] QQ:{at_sender}" if at_found else "[--] 未匹配"
        result_str = "[TRIGGER] 触发道馆!" if result else "[--] 未满足"
        print(f"  => 关键词: {kw_str} | @全体: {at_str} | 结果: {result_str}")
        print()


if __name__ == '__main__':
    if test_api():
        filtered = test_messages()
        test_sender_scenarios(filtered)
