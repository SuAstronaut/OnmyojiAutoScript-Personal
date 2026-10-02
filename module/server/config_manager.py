# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import copy
import json
import re
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ValidationError

from module.config.config_model import ConfigModel
from module.config.config_store import (
    ConfigNotFoundError,
    ConfigStore,
)
from module.config.config_generation import (
    ConfigGenerationError,
    ConfigIdentityConflictError,
)
from module.config.config_validation import (
    DEFAULT_CONFIG_PROFILE,
    STRICT_CONFIG_VALIDATION,
    ConfigValidationError as StrictConfigValidationError,
    _reject_unknown_keys,
    _sort_dynamic_member_keys,
    normalize_legacy_config,
)
from module.config.utils import convert_to_underscore
from module.logger import logger


CONFIG_NAME_RESERVED_CHARS = set('/\\:*?"<>|')
CONFIG_TASK_TRANSFER_EXCLUDED_KEYS = {
    "config_name",
    "running_task",
}
CONFIG_REDACTION_VALUE = "XXX"
CONFIG_REDACTION_PATHS = (
    "wanted_quests.wanted_quests_config.invite_friend_name",
    "*.invite_config.friend_list",
    "script.error.notify_config",
    "global_game.server.password",
    "script.device.serial",
    "script.device.handle",
    "script.device.emulatorinfo_name",
    "script.device.emulatorinfo_path",
    "find_jade.sup_account_list_*.account",
    "find_jade.sup_account_list_*.account_alias",
)
CONFIG_REDACTION_KEYS = {
    "password",
    "token",
    "access_token",
    "cookie",
    "authorization",
}


class ConfigNameError(ValueError):
    """配置名称不合法。"""


class ConfigAlreadyExistsError(FileExistsError):
    """导入目标配置已存在。"""


class ConfigNotFoundError(FileNotFoundError):
    """配置文件不存在。"""


class ConfigJsonError(ValueError):
    """配置 JSON 无法解析。"""


class ConfigTaskError(ValueError):
    """配置任务名称或任务 JSON 不合法。"""


class ConfigValidationError(ValueError):
    """配置内容不符合当前 ConfigModel。"""

    def __init__(self, fields: list[dict[str, str]]) -> None:
        super().__init__("Config validation failed")
        self.fields = fields

class ConfigManager:
    def __init__(self, store: ConfigStore = None) -> None:
        # 构造本身不做任何 I/O；MainManager 复用同一 store，任务 3 起全部实例配置访问走 Store。
        self.store = store or ConfigStore(config_root=Path.cwd() / 'config')

    def config_dir(self) -> Path:
        return self.store.config_root

    def config_path(self, name: str) -> Path:
        return self.store.config_root / f'{name}.json'

    @staticmethod
    def validate_config_name(name: str, *, allow_template: bool = True) -> str:
        """
        校验配置名称，返回去除首尾空白后的名称。
        """
        name = (name or '').strip()
        if not name:
            raise ConfigNameError("Config name is required")
        if not allow_template and name == 'template':
            raise ConfigNameError("Config name template is reserved")
        if '.' in name:
            raise ConfigNameError("Config name cannot contain dots")
        if any(ch in CONFIG_NAME_RESERVED_CHARS for ch in name):
            raise ConfigNameError("Config name contains reserved path characters")
        if any(ord(ch) < 32 for ch in name):
            raise ConfigNameError("Config name contains control characters")
        return name

    @staticmethod
    def _format_validation_error(error: ValidationError) -> list[dict[str, str]]:
        fields = []
        for item in error.errors():
            loc = item.get("loc", ())
            field = ".".join(str(part) for part in loc) if loc else "__root__"
            fields.append(
                {
                    "field": field,
                    "message": item.get("msg", ""),
                    "type": item.get("type", ""),
                }
            )
        return fields

    @staticmethod
    def _format_field_error(field: str, message: str, error_type: str) -> dict[str, str]:
        return {
            "field": field,
            "message": message,
            "type": error_type,
        }

    @staticmethod
    def _is_model_type(annotation: Any) -> bool:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    @staticmethod
    def _list_item_model(annotation: Any) -> type[BaseModel] | None:
        origin = get_origin(annotation)
        if origin is not list:
            return None
        args = get_args(annotation)
        if not args:
            return None
        item_type = args[0]
        return item_type if ConfigManager._is_model_type(item_type) else None

    @staticmethod
    def _dynamic_list_item_model(key: str, fields: dict[str, Any]) -> tuple[str, type[BaseModel]] | None:
        for field_name, field_info in fields.items():
            if not re.fullmatch(rf'{re.escape(field_name)}_\d+', key):
                continue
            item_model = ConfigManager._list_item_model(field_info.annotation)
            if item_model is not None:
                return field_name, item_model
        return None

    @staticmethod
    def _join_field_path(prefix: str, key: str) -> str:
        return f'{prefix}.{key}' if prefix else key

    @staticmethod
    def _collect_dynamic_field_validation_errors(
        data: Any,
        model_type: type[BaseModel],
        prefix: str = '',
    ) -> list[dict[str, str]]:
        if not isinstance(data, dict):
            return []

        errors = []
        fields = model_type.model_fields
        for key, value in data.items():
            field_path = ConfigManager._join_field_path(prefix, str(key))
            dynamic_field = ConfigManager._dynamic_list_item_model(str(key), fields)
            if dynamic_field is not None:
                _, item_model = dynamic_field
                try:
                    item_model(**value)
                except ValidationError as e:
                    for error in ConfigManager._format_validation_error(e):
                        error["field"] = ConfigManager._join_field_path(field_path, error["field"])
                        errors.append(error)
                except TypeError as e:
                    errors.append(ConfigManager._format_field_error(field_path, str(e), 'model_type'))
                continue

            if key not in fields:
                continue
            annotation = fields[key].annotation
            if ConfigManager._is_model_type(annotation):
                errors.extend(ConfigManager._collect_dynamic_field_validation_errors(value, annotation, field_path))
                continue
            item_model = ConfigManager._list_item_model(annotation)
            if item_model is not None and isinstance(value, list):
                for index, item in enumerate(value):
                    errors.extend(
                        ConfigManager._collect_dynamic_field_validation_errors(item, item_model, f'{field_path}.{index}')
                    )
        return errors

    @staticmethod
    def validate_task_key(task_name: str) -> str:
        """
        校验并归一化配置任务名称。
        """
        task_key = convert_to_underscore((task_name or '').strip())
        if not task_key:
            raise ConfigTaskError("Task name is required")
        if task_key in CONFIG_TASK_TRANSFER_EXCLUDED_KEYS:
            raise ConfigTaskError(f'Task cannot be transferred: {task_key}')
        if task_key not in ConfigModel.model_fields:
            raise ConfigTaskError(f'Task not found: {task_key}')
        if not ConfigManager._is_model_type(ConfigModel.model_fields[task_key].annotation):
            raise ConfigTaskError(f'Task is not transferable: {task_key}')
        return task_key

    @staticmethod
    def parse_task_json_source(
        *,
        json_text: str | None = None,
        file_content: bytes | None = None,
    ) -> dict[str, Any]:
        """
        解析任务 JSON 输入，json_text 和 file_content 必须二选一。
        """
        has_json_text = json_text is not None
        has_file_content = file_content is not None
        if has_json_text == has_file_content:
            raise ConfigJsonError("Exactly one of json_text or file must be provided")

        if has_file_content:
            try:
                text = file_content.decode('utf-8')
            except UnicodeDecodeError as e:
                raise ConfigJsonError(f'Task JSON file must be UTF-8 JSON: {e}') from e
        else:
            text = json_text

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigJsonError(f'Task JSON parse failed: {e}') from e

        if not isinstance(data, dict):
            raise ConfigJsonError("Task JSON root must be an object")
        return data

    @staticmethod
    def validate_task_import_payload(task_key: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        校验导入任务 JSON 的顶层结构，并返回对应任务 value。
        """
        if len(data) != 1:
            raise ConfigJsonError("Task JSON root must contain exactly one task key")
        payload_task_key, task_value = next(iter(data.items()))
        if payload_task_key != task_key:
            raise ConfigJsonError(f'Task JSON key mismatch: expected {task_key}, got {payload_task_key}')
        if not isinstance(task_value, dict):
            raise ConfigJsonError("Task JSON value must be an object")
        return task_value

    @staticmethod
    def _prefix_validation_error(error: dict[str, str], prefix: str) -> dict[str, str]:
        field = error.get("field", "")
        error["field"] = ConfigManager._join_field_path(prefix, field) if field else prefix
        return error

    @staticmethod
    def validate_task_value(task_key: str, task_value: dict[str, Any]) -> dict[str, Any]:
        """迁移旧字段并补齐任务默认值，返回交给 Store 做最终校验的数据。

        废弃字段使用持久化层的同一规则剔除，不再额外拒绝。模型仍负责新字段和
        动态列表的默认补全，避免直接把旧任务片段交给严格存储时误判为缺项。
        """
        task_model_type = ConfigModel.model_fields[task_key].annotation
        fields = []

        try:
            # 迁移会深拷贝输入；保留任务根路径，复用改名字段、旧枚举及动态字段的规则。
            task_value = normalize_legacy_config({task_key: task_value}, "")[task_key]
            _reject_unknown_keys(task_value, task_model_type, DEFAULT_CONFIG_PROFILE, (task_key,))
            # 导入 JSON 的键顺序不代表账号顺序，补默认前按成员编号排列，避免账号错位。
            _sort_dynamic_member_keys({task_key: task_value}, DEFAULT_CONFIG_PROFILE)
            # 部分任务的 before-validator 会吞掉坏列表项，仍需在补默认之前显式拦截。
            fields.extend(ConfigManager._collect_dynamic_field_validation_errors(
                task_value, task_model_type, task_key,
            ))
            # 任务导入属于持久化边界，禁止 ConfigBase 把越界值静默回退为默认值。
            token = STRICT_CONFIG_VALIDATION.set(True)
            try:
                task_model = task_model_type.model_validate(task_value)
            finally:
                STRICT_CONFIG_VALIDATION.reset(token)
        except ValidationError as e:
            for error in ConfigManager._format_validation_error(e):
                fields.append(ConfigManager._prefix_validation_error(error, task_key))
            task_model = None
        except (TypeError, ValueError, AttributeError) as e:
            # 与整份配置导入保持一致，任务/分组形状异常必须返回可映射到 HTTP 400 的错误。
            fields.append(ConfigManager._format_field_error(
                task_key, str(e), "model_type",
            ))
            task_model = None

        if fields:
            raise ConfigValidationError(fields)
        return task_model.model_dump(mode="json")

    def import_task_config(self, name: str, task_name: str, data: dict[str, Any]) -> tuple[str, str]:
        """
        导入单个任务配置，返回配置名称和归一化任务 key。
        通过 ConfigStore 的 REPLACE_SUBTREE 原子替换目标任务子树。
        """
        name = self.validate_config_name(name, allow_template=False)
        task_key = self.validate_task_key(task_name)
        task_value = self.validate_task_import_payload(task_key, data)
        validated_task_value = self.validate_task_value(task_key, task_value)
        loaded = self.store.load(name)
        canonical = loaded.canonical
        if task_key not in canonical:
            raise ConfigNotFoundError(f'Task not found in config: {task_key}')
        expected = canonical[task_key]
        try:
            self.store.replace_subtree(
                name,
                (task_key,),
                expected,
                validated_task_value,
                loaded.generation,
            )
        except StrictConfigValidationError as e:
            # 完整配置的最终校验（如列表数量一致性）也返回可读的 400，失败不写盘。
            raise ConfigValidationError([
                self._format_field_error(task_key, str(e), "config_validation"),
            ]) from e
        logger.info(f'import task {task_key} to {name}')
        return name, task_key

    def load_task_for_transfer(self, name: str, task_name: str, *, allow_template: bool = True) -> tuple[str, str, dict[str, Any]]:
        """
        读取单个任务配置片段，返回配置名、任务 key、任务 JSON。
        """
        name, data = self.load_config_for_export(name)
        if not allow_template and name == 'template':
            raise ConfigNameError("Config name template is reserved")
        task_key = self.validate_task_key(task_name)
        if task_key not in data:
            raise ConfigNotFoundError(f'Task not found in config: {task_key}')
        task_value = data[task_key]
        if not isinstance(task_value, dict):
            raise ConfigJsonError("Task JSON value must be an object")
        return name, task_key, {task_key: copy.deepcopy(task_value)}

    def load_task_for_export(self, name: str, task_name: str) -> tuple[str, str, dict[str, Any]]:
        """
        读取脱敏后的单个任务配置片段。
        """
        name, data = self.load_config_for_export(name)
        task_key = self.validate_task_key(task_name)
        if task_key not in data:
            raise ConfigNotFoundError(f'Task not found in config: {task_key}')
        redacted = self.redact_config(data)
        task_value = redacted.get(task_key)
        if not isinstance(task_value, dict):
            raise ConfigJsonError("Task JSON value must be an object")
        return name, task_key, {task_key: copy.deepcopy(task_value)}

    def import_config(self, name: str, data: dict[str, Any]) -> str:
        """
        导入配置内容，返回最终配置名称。
        """
        name = self.validate_config_name(name, allow_template=False)
        if not isinstance(data, dict):
            raise ConfigJsonError("Config JSON root must be an object")
        try:
            self.store.import_config(name, data)
        except ConfigIdentityConflictError as e:
            raise ConfigAlreadyExistsError(str(e))
        except ConfigGenerationError:
            # 身份损坏、migration/recovery 失败不是“目标已存在”，保留为服务端错误。
            raise
        except StrictConfigValidationError as e:
            # 严格持久化校验失败：统一映射到 API 的 400 字段结构
            raise ConfigValidationError([self._format_field_error("__root__", str(e), "config_validation")])
        logger.info(f'import config {name}')
        return name

    def load_config_for_export(self, name: str) -> tuple[str, dict[str, Any]]:
        """
        读取待导出的配置，返回校验后的名称和配置内容。
        """
        name = self.validate_config_name(name, allow_template=True)
        data = self.store.load_canonical_snapshot(name)
        return name, data

    @staticmethod
    def redact_config(data: dict[str, Any]) -> dict[str, Any]:
        """
        返回脱敏后的配置副本，不修改传入对象。
        """
        redacted = copy.deepcopy(data)
        for rule in CONFIG_REDACTION_PATHS:
            ConfigManager._redact_by_path(redacted, rule.split('.'))
        ConfigManager._redact_by_key(redacted)
        return redacted

    @staticmethod
    def _segment_match(key: str, segment: str) -> bool:
        if segment == '*':
            return True
        if segment.endswith('*'):
            return key.startswith(segment[:-1])
        return key == segment

    @staticmethod
    def _redact_by_path(node: Any, segments: list[str]) -> None:
        if not segments or not isinstance(node, dict):
            return
        segment = segments[0]
        is_leaf = len(segments) == 1
        for key, value in node.items():
            if not ConfigManager._segment_match(str(key), segment):
                continue
            if is_leaf:
                node[key] = CONFIG_REDACTION_VALUE
            else:
                ConfigManager._redact_by_path(value, segments[1:])

    @staticmethod
    def _redact_by_key(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in CONFIG_REDACTION_KEYS:
                    node[key] = CONFIG_REDACTION_VALUE
                else:
                    ConfigManager._redact_by_key(value)
        elif isinstance(node, list):
            for item in node:
                ConfigManager._redact_by_key(item)

    def all_script_files(self) -> list[str]:
        """
        获取所有的脚本文件 除了template
        :return: ['oas1', 'oas2']
        """
        result = self.store.active_config_names()
        if len(result) == 0:
            # 如果没有活动实例则基于 template 创建一个 oas1
            template = self.store.load('template').canonical
            self.store.create_from_template('oas1', copy.deepcopy(template))
            result = self.store.active_config_names()
        return result

    def all_json_file(self) -> list:
        """
        获取所有的json文件
        :return: ['oas1', 'oas2']
        """
        result = self.store.active_config_names(include_template=True)
        if 'template' in result:
            result.remove('template')
            result.insert(0, 'template')
        return result

    def copy(self, file: str, template: str = 'template') -> None:
        """
        复制一个配置文件；生命周期异常必须交给 API 层按类别映射，不能静默成功。
        :param file:  不带json后缀
        :param template:
        :return:
        """
        file = self.validate_config_name(file, allow_template=False)
        template = self.validate_config_name(template, allow_template=True)
        canonical = self.store.load(template).canonical
        self.store.create_from_template(file, copy.deepcopy(canonical))
        logger.info(f'copy {template} to {file}')

    def generate_script_name(self) -> str:
        """
        生成一个新的配置的名字
        :return:
        """
        all_script_files = self.all_script_files()
        if not all_script_files:
            return 'oas1'

        script_numbers = []
        for script_file in all_script_files:
            match = re.search(r'\d+', script_file)
            if match:
                script_number = int(match.group())
                script_numbers.append(script_number)

        if not script_numbers:
            return 'oas1'
        script_numbers.sort()
        new_script_number = script_numbers[-1] + 1
        return f'oas{new_script_number}'
