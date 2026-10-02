"""测试 NapCat get_group_msg_history 翻页行为"""
import requests, json

# 按你自己机器的 NapCat 部署填写；令牌与群号属于私密信息，不要提交进仓库
token = ''
endpoint = 'http://127.0.0.1:3000'
headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
group_id = 0

def fetch_page(message_seq=0, count=5):
    body = {'group_id': group_id, 'message_seq': message_seq, 'count': count}
    r = requests.post(f'{endpoint}/get_group_msg_history', json=body, headers=headers, timeout=10)
    return r.json().get('data', {}).get('messages', [])

# 第1页
msgs1 = fetch_page(0, 5)
print(f'=== 第1页: {len(msgs1)}条 ===')
for m in msgs1:
    t = m.get('time', 0)
    mid = m.get('message_id', 0)
    seq = m.get('message_seq', 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''
    print(f'  time={ts}, msg_id={mid}, msg_seq={seq}')

if not msgs1:
    print('无消息，退出')
    exit()

# 第2页: 用第1页最后一条的message_seq
last_seq = msgs1[-1].get('message_seq', 0)
print(f'\n=== 第2页: 用最后一条 message_seq={last_seq} ===')
msgs2 = fetch_page(last_seq, 5)
for m in msgs2:
    t = m.get('time', 0)
    mid = m.get('message_id', 0)
    seq = m.get('message_seq', 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''
    print(f'  time={ts}, msg_id={mid}, msg_seq={seq}')

ids1 = set(m.get('message_id') for m in msgs1)
ids2 = set(m.get('message_id') for m in msgs2)
print(f'重叠: {len(ids1 & ids2)}条')

# 第2页alt: 用第1页最早时间消息的message_seq
earliest = min(msgs1, key=lambda m: m.get('time', 0))
earliest_seq = earliest.get('message_seq', 0)
print(f'\n=== 第2页alt: 用时间最早消息 message_seq={earliest_seq} ===')
msgs3 = fetch_page(earliest_seq, 5)
for m in msgs3:
    t = m.get('time', 0)
    mid = m.get('message_id', 0)
    seq = m.get('message_seq', 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''
    print(f'  time={ts}, msg_id={mid}, msg_seq={seq}')

ids3 = set(m.get('message_id') for m in msgs3)
print(f'与第1页重叠: {len(ids1 & ids3)}条')

# 第2页alt2: 用更大的count看看能不能拿到更多历史
print(f'\n=== 第2页alt2: message_seq=0, count=20 ===')
msgs4 = fetch_page(0, 20)
for m in msgs4:
    t = m.get('time', 0)
    mid = m.get('message_id', 0)
    seq = m.get('message_seq', 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''
    print(f'  time={ts}, msg_id={mid}, msg_seq={seq}')

# 用第1页第一条(最新)的message_seq翻页
first_seq = msgs1[0].get('message_seq', 0)
print(f'\n=== 第2页alt3: 用第一条(最新) message_seq={first_seq} ===')
msgs5 = fetch_page(first_seq, 5)
for m in msgs5:
    t = m.get('time', 0)
    mid = m.get('message_id', 0)
    seq = m.get('message_seq', 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(t).strftime('%H:%M:%S') if t else ''
    print(f'  time={ts}, msg_id={mid}, msg_seq={seq}')
ids5 = set(m.get('message_id') for m in msgs5)
print(f'与第1页重叠: {len(ids1 & ids5)}条, 新消息: {len(ids5 - ids1)}条')
