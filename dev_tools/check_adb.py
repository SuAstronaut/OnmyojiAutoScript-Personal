# -*- coding: utf-8 -*-
"""
ADB 环境自检脚本
================
复现 module/device/connection_attr.py 中 ConnectionAttr.adb_binary 的查找逻辑，
逐个检查所有候选 adb.exe 路径是否存在，并给出结论与修复建议。

纯标准库实现，不依赖项目任何模块，可在报错环境下直接运行：
    ./toolkit/python.exe dev_tools/check_adb.py
"""
import os
import shutil
import sys

# 与 connection_attr.py 中 adb_binary_list 保持一致
ADB_BINARY_LIST = [
    './bin/adb/adb.exe',
    './toolkit/Lib/site-packages/adbutils/binaries/adb.exe',
    '/usr/bin/adb',
]


def normalize(path: str) -> str:
    """统一为绝对路径并转换分隔符，避免相对路径歧义。"""
    return os.path.abspath(path).replace('\\', '/')


def collect_candidates() -> list:
    """收集所有候选 adb 路径，顺序与 ConnectionAttr.adb_binary 一致。"""
    candidates = []

    # 第 1 步：由当前 python 解释器推导的 adbutils 自带 adb
    # 用项目自带 python (toolkit/python.exe) 启动时即 toolkit/Lib/.../adb.exe
    derived = os.path.join(sys.executable, '../Lib/site-packages/adbutils/binaries/adb.exe')
    candidates.append(('adbutils 自带(由 sys.executable 推导)', normalize(derived)))

    # 第 2 步：固定的候选路径列表
    for p in ADB_BINARY_LIST:
        candidates.append((p, normalize(p)))

    # 第 3 步：系统 PATH 中的 adb
    which = shutil.which('adb')
    if which:
        candidates.append(('系统 PATH (shutil.which)', normalize(which)))

    return candidates


def main() -> None:
    # 打印运行环境，便于定位问题
    print(f'当前解释器: {sys.executable}')
    print(f'当前工作目录: {os.getcwd()}')
    print('-' * 60)

    found = None
    for label, path in collect_candidates():
        exists = os.path.exists(path)
        marker = '[OK]' if exists else '[MISS]'
        print(f'{marker} {label}: {path}')
        if exists and found is None:
            found = path

    print('-' * 60)
    if found:
        print(f'找到 adb: {found}')
    else:
        print('未找到任何 adb，请按以下任一方式修复：')
        print('  1. 在项目根目录使用内置 python 启动: ./toolkit/python.exe gui.py')
        print('  2. 缺失 adbutils 则重装: ./toolkit/python.exe -m pip install --no-cache-dir adbutils')
        print('  3. 手动放置 adb.exe 到 ./bin/adb/ ，或把 adb 加入系统 PATH')
        # 与 connection_attr.py 一致的行为提示
        print('脚本会因此报错: No adb binary found, please check your environment')


if __name__ == '__main__':
    main()
