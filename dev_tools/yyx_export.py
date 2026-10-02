"""账号导出组件安装与单账号验证入口，不启动 OAS 日常或切换角色。"""
import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.request import Request, urlopen

from tasks.Utils.yyx_export import (
    FRIDA_VERSION, ROOT, SERVER_NAME, SERVER_PATH, export_account_snapshot,
)


# 固定官方版本及归档摘要，避免下载失败或文件被替换后仍当作可执行组件。
ARCHIVE_SHA256 = '4070285722520c19a28a778a2fe0597fffbedd19f3b829b1374ac2616b227063'


def install_component():
    """只安装缺少的设备端组件，项目已包含匹配的 Frida Python 依赖。"""
    if SERVER_PATH.is_file():
        print(f'账号导出组件已存在：{SERVER_PATH}')
        return
    url = f'https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/{SERVER_NAME}.xz'
    print('正在下载账号导出组件……')
    with urlopen(Request(url, headers={'User-Agent': 'OAS-YYX-Export'}), timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('账号导出组件校验失败，未安装')
    data = lzma.decompress(archive)
    SERVER_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=SERVER_PATH.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, SERVER_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'账号导出组件已安装：{SERVER_PATH}')


def main():
    """读取指定配置的连接信息，绝不从多个在线设备中猜选导出对象。"""
    parser = argparse.ArgumentParser(description='安装 YYX 导出组件或导出当前已登录的角色')
    parser.add_argument('--install', action='store_true', help='安装账号导出组件')
    parser.add_argument('--config', help='OAS 配置名，例如 oas2')
    args = parser.parse_args()
    if args.install:
        install_component()
        return
    if not args.config or Path(args.config).name != args.config or args.config in ('.', '..'):
        parser.error('请传入 --config 配置名')
    path = ROOT / 'config' / f'{args.config}.json'
    config = json.loads(path.read_text(encoding='utf-8'))['script']['device']
    device = SimpleNamespace(serial=config['serial'], package=config.get('package_name', 'auto'))
    # 命令行没有账号上下文，导出当前登录角色且不写来源身份。
    result = export_account_snapshot(device, args.config)
    print('账号数据导出成功：' + result['path'])
    print('数据条数：' + json.dumps(result['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()
