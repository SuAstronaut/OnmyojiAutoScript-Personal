import json
import os
from pathlib import Path

from module.logger import logger
from tasks.Utils.optional_tasks import extend

# 候选项在运行时按环境现算、值不稳定的字段，其 enumEnum 不参与翻译 key 收集。
# handle 与 leader_instance 都由运行环境动态枚举，不应把候选值写进翻译表。
DYNAMIC_ENUM_FIELDS = frozenset({'handle', 'leader_instance'})

# 整组条目都由运行环境现算的参数组：组内条目的 name 是用户实例名的哈希
# （config_<sha256>），description 里还带明文实例名与账号数量
# （如「某个实例名 账号总数：42」）。收进翻译表等于把用户数据写进随仓库分发的
# assets/i18n/zh-CN.json，且用户每新增一个实例就追加一批既无法复用、也无法
# 自动清理的条目。组名本身仍是稳定 key（前端要显示分组标题），故只跳过组内条目。
DYNAMIC_ITEM_GROUPS = frozenset({'account_config_selection'})


class Addition:
    # 补充翻译目录（下发给 OASX 的翻译源），类属性便于测试替换路径。
    # 注意：与既有 file_zh_cn 一致，import 时基于 Path.cwd() 固化，
    # 要求后端进程以仓库根为工作目录启动
    assets_i18n_dir = Path.cwd() / 'assets' / 'i18n'

    @classmethod
    def load_additions(cls) -> dict:
        result = {}
        files: str = ['en-US', 'zh-CN']
        for file in files:
            file_path = cls.assets_i18n_dir / f'{file}.json'
            result[file] = {}
            if not file_path.exists():
                continue
            with open(str(file_path), 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 过滤空值与非字符串值：兼容历史空值占位条目与手工误填的非字符串值；
            # 前端 .tr 查不到会回退显示 key 原文，避免显示空白
            result[file] = {k: v for k, v in data.items() if isinstance(v, str) and v}
        # 本机翻译只在下发时合并，不写入随仓库分发的翻译表。
        return extend('translations', result, cls.assets_i18n_dir)


class I18n(Addition):
    file_zh_cn = Path.cwd() / 'module' / 'config' / 'i18n' / 'zh-CN.json'

    @classmethod
    def trans_zh_cn(cls, text) -> str:
        cn_zh_data = cls.load_zh_cn()
        return cn_zh_data[text] if text in cn_zh_data else text

    @classmethod
    def save_zh_cn(cls, data) -> None:
        I18n.file_zh_cn.parent.mkdir(parents=True, exist_ok=True)
        with open(str(I18n.file_zh_cn), 'w', encoding='utf-8') as f:
            s = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=str)
            f.write(s)

    @classmethod
    def load_zh_cn(cls) -> dict:
        if not I18n.file_zh_cn.exists():
            return {}
        with open(str(I18n.file_zh_cn), 'r', encoding='utf-8') as f:
            return json.load(f)

    @classmethod
    def collect_frontend_keys(cls, menu: dict, script_task_fn) -> set:
        """收集前端(OASX)渲染时实际会查翻译表的全部 key

        :param menu: gui_menu_list 返回的 {菜单分组名: [任务名]}
        :param script_task_fn: ConfigModel.script_task 的可调用对象
        """
        # 本机任务的 Schema 不参与公共翻译补齐，防止同步命令重新泄漏名称。
        menu = extend('translation_menu', menu)
        keys = set()
        for group_name, task_names in menu.items():
            keys.add(group_name)
            for task_name in task_names:
                keys.add(task_name)
                # 单个任务 schema 获取或消费异常都只跳过该任务，不影响其余任务收集
                try:
                    task_args = script_task_fn(task_name)
                    for arg_group, items in task_args.items():
                        keys.add(arg_group)
                        # 动态参数组只收组名，组内条目含用户实例名与账号数量
                        if arg_group in DYNAMIC_ITEM_GROUPS:
                            continue
                        for item in items:
                            # 前端 ArgumentModel.title 取的是 name 字段（不是 title）
                            keys.add(item['name'])
                            # description 为空字符串时跳过，避免产生空 key 垃圾条目
                            if item.get('description'):
                                keys.add(item['description'])
                            # 动态字段的候选项按运行时环境现算（如 handle 是已开
                            # 客户端窗口，值里含每次开游戏都变的 PID），收进翻译表
                            # 只会不断追加无法复用、也无法自动清理的垃圾条目
                            if item['name'] in DYNAMIC_ENUM_FIELDS:
                                continue
                            for enum_value in item.get('enumEnum', []):
                                if isinstance(enum_value, str):
                                    keys.add(enum_value)
                except Exception as e:
                    logger.warning(f'i18n sync: collect keys of task [{task_name}] failed: {e}')
                    continue
        # 兜底丢弃空字符串 key（无效翻译 key）
        keys.discard('')
        return keys

    @classmethod
    def sync_missing_keys(cls, menu: dict, script_task_fn) -> int:
        """启动时把前端会用到但两侧翻译都缺失的 key 以 key 原文为占位值补进 assets/i18n/zh-CN.json

        开发者随后只需在该文件填中文即可，OASX 无需改代码。
        返回本次补齐的 key 数量；守护分支命中时返回 0 且不写文件。
        """
        # 镜像文件（OASX 上传的前端内置翻译）不存在说明前端从未连接过，
        # 无法区分“前端已内置”与“真缺失”，跳过以免灌入大量误报条目
        if not cls.file_zh_cn.exists():
            logger.warning('i18n sync skipped: frontend mirror translation not found')
            return 0
        mirror = cls.load_zh_cn()

        addition_file = cls.assets_i18n_dir / 'zh-CN.json'
        additions = {}
        if addition_file.exists():
            try:
                with open(str(addition_file), 'r', encoding='utf-8') as f:
                    additions = json.load(f)
            except json.JSONDecodeError as e:
                # 文件损坏时不写回，保护人工翻译成果
                logger.error(f'i18n sync skipped: {addition_file} is broken: {e}')
                return 0

        keys = cls.collect_frontend_keys(menu, script_task_fn)
        missing = sorted(k for k in keys if k not in additions and k not in mirror)
        if not missing:
            logger.info('i18n sync: no missing keys')
            return 0
        # 追加到尾部：dict 保序，既有条目顺序不变；占位值为 key 原文（显示效果与
        # 未翻译时一致），等待人工改成中文
        for key in missing:
            additions[key] = key
        addition_file.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换，进程中途崩溃不会截断正式文件（内含人工翻译成果）
        tmp_file = addition_file.with_suffix('.json.tmp')
        with open(str(tmp_file), 'w', encoding='utf-8') as f:
            json.dump(additions, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(str(tmp_file), str(addition_file))
        logger.info(f'i18n sync: {len(missing)} missing keys appended to {addition_file}')
        return len(missing)


if __name__ == '__main__':
    print(I18n.load_zh_cn())
