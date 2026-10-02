import sys
import typing as t

from pydantic import BaseModel

from module.base.decorator import cached_property, del_cached_property
from module.base.utils import SelectedGrids
# from module.device.connection import Connection
from module.device.method.utils import get_serial_pair
from module.device.platform2.emulator_base import EmulatorInstanceBase, EmulatorManagerBase, remove_duplicated_path
from module.logger import logger


class EmulatorInfo(BaseModel):
    emulator: str = ''
    name: str = ''
    path: str = ''

    # For APIs of chinac.com, a phone cloud platform.
    # access_key: SecretStr = ''
    # secret: SecretStr = ''


class PlatformBase(EmulatorManagerBase):
    """
    Base interface of a platform, platform can be various operating system or phone clouds.
    For each `Platform` class, the following APIs must be implemented.
    - all_emulators()
    - all_emulator_instances()
    - emulator_start()
    - emulator_stop()
    """

    # cancel_event: 可选取消信号（threading.Event）。Web 标注采集线程断开时置位，
    # 用于在模拟器拉起链路（full_recovery / emulator_start_watch）的检查点尽快放弃拉起。
    # 默认 None 表示从不取消，行为与原先一致。
    _cancel_event = None

    def _is_cancelled(self) -> bool:
        # 仅当注入了 cancel_event 且已被置位时返回 True；未注入时恒为 False（零回归）。
        # 用 getattr 兜底，防御未经 __init__ 构造的实例（如测试夹具）。
        cancel_event = getattr(self, '_cancel_event', None)
        return cancel_event is not None and cancel_event.is_set()

    def emulator_start(self):
        """
        Start a emulator, until startup completed.
        - Retry is required.
        - Using bored sleep to wait startup is forbidden.
        """
        logger.info(f'Current platform {sys.platform} does not support emulator_start, skip')

    def emulator_stop(self):
        """
        Stop a emulator.
        """
        logger.info(f'Current platform {sys.platform} does not support emulator_stop, skip')

    @cached_property
    def config_interface(self) -> dict:
        result = {'emulator': 'auto',
                  'name': '',
                  'path': '',
                  'serial': '127.0.0.1:16384'}
        if hasattr(self, 'config'):
            result = {
                'emulator': self.config.script.device.emulatorinfo_type,
                'name': self.config.script.device.emulatorinfo_name,
                'path': self.config.script.device.emulatorinfo_path,
                'serial': self.config.script.device.serial
            }
        return result

    def _config_save_new(self, emulator: str, name: str, path: str):
        if hasattr(self, 'config'):
            # 通过 startup_normalize 只把声明路径合入 provisional COLD 快照与 session model/base，
            # 调用方不得预改 model；正式快照冻结后禁止调用。
            updates = {}
            if self.config.script.device.emulatorinfo_type != emulator:
                updates[("script", "device", "emulatorinfo_type")] = emulator
            if self.config.script.device.emulatorinfo_name != name:
                updates[("script", "device", "emulatorinfo_name")] = name
            if self.config.script.device.emulatorinfo_path != path:
                updates[("script", "device", "emulatorinfo_path")] = path
            if updates:
                self.config.startup_normalize(updates)

    @cached_property
    def emulator_info(self) -> EmulatorInfo:
        emulator = self.config_interface['emulator']
        if emulator == 'auto':
            emulator = ''

        def parse_info(value):
            if isinstance(value, str):
                value = value.strip().replace('\n', '')
                if value in ['None', 'False', 'True']:
                    value = ''
                return value
            else:
                return ''

        name = parse_info(self.config_interface['name'])
        path = parse_info(self.config_interface['path'])

        return EmulatorInfo(
            emulator=emulator,
            name=name,
            path=path,
        )

    @cached_property
    def emulator_instance(self) -> t.Optional[EmulatorInstanceBase]:
        """
        Returns:
            EmulatorInstanceBase: Emulator instance or None
        """
        data = self.emulator_info
        old_info = dict(
            emulator=data.emulator,
            path=data.path,
            name=data.name,
        )
        # Redirect emulator-5554 to 127.0.0.1:5555
        serial = self.config_interface['serial']
        port_serial, _ = get_serial_pair(serial)
        if port_serial is not None:
            serial = port_serial

        instance = self.find_emulator_instance(
            serial=serial,
            name=data.name,
            path=data.path,
            emulator=data.emulator,
        )

        # Write complete emulator data
        if instance is not None:
            new_info = dict(
                emulator=instance.type,
                path=instance.path,
                name=instance.name,
            )
            if new_info != old_info:
                self._config_save_new(emulator=instance.type, name=instance.name, path=instance.path)
                del_cached_property(self, 'emulator_info')

        return instance

    def find_emulator_instance(
            self,
            serial: str,
            name: str = None,
            path: str = None,
            emulator: str = None
    ) -> t.Optional[EmulatorInstanceBase]:
        """
        Args:
            serial: Serial like "127.0.0.1:5555"
            name: Instance name like "Nougat64"
            path: Emulator install path like "C:/Program Files/BlueStacks_nxt/HD-Player.exe"
            emulator: Emulator type defined in Emulator class, like "BlueStacks5"

        Returns:
            EmulatorInstance: Emulator instance or None if no instances not found.
        """
        logger.hr('Find emulator instance', level=2)
        instances = SelectedGrids(self.all_emulator_instances)
        for instance in instances:
            logger.info(instance)
        search_args = dict(serial=serial)

        instance_id = serial_to_id(self.serial)
        if instance_id is not None:
            select = instances.select(MuMuPlayer12_id=instance_id)
            # No logs for if select.count == 1:
            # because this is just a trial
            if select.count == 1:
                instance = select[0]
                logger.hr('Emulator instance', level=2)
                logger.info(f'Found emulator instance: {instance}')
                return instance
        # Search by serial
        select = instances.select(**search_args)
        if select.count == 0:
            # 桥接模式下 serial 可能为局域网 IP，与实例记录的 NAT serial 不匹配
            # 回退到使用 name/path/emulator 匹配
            logger.info(f'No emulator instance with {search_args}, trying to match by name/path/emulator')
            search_args = dict()
        elif select.count == 1:
            instance = select[0]
            logger.hr('Emulator instance', level=2)
            logger.info(f'Found emulator instance: {instance}')
            return instance

        # Multiple instances in given serial, search by name
        if name:
            search_args['name'] = name
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'No emulator instances with {search_args}, name invalid')
                search_args.pop('name')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Emulator instance', level=2)
                logger.info(f'Found emulator instance: {instance}')
                return instance

        # Multiple instances in given serial and name, search by path
        if path:
            search_args['path'] = path
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'No emulator instances with {search_args}, path invalid')
                search_args.pop('path')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Emulator instance', level=2)
                logger.info(f'Found emulator instance: {instance}')
                return instance

        # Multiple instances in given serial, name and path, search by emulator
        if emulator:
            search_args['type'] = emulator
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'No emulator instances with {search_args}, type invalid')
                search_args.pop('type')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Emulator instance', level=2)
                logger.info(f'Found emulator instance: {instance}')
                return instance

        # Still too many instances, search from running emulators
        running = remove_duplicated_path(list(self.iter_running_emulator()))
        logger.info('Running emulators')
        for exe in running:
            logger.info(exe)
        if len(running) == 1:
            logger.info('Only one running emulator')
            # Same as searching path
            search_args['path'] = running[0]
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'No emulator instances with {search_args}, path invalid')
                search_args.pop('path')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Emulator instance', level=2)
                logger.info(f'Found emulator instance: {instance}')
                return instance

        # Still too many instances
        logger.warning(f'Found multiple emulator instances with {search_args}')
        return None

def serial_to_id(serial: str):
    """
    Predict instance ID from serial
    E.g.
        "127.0.0.1:16384" -> 0
        "127.0.0.1:16416" -> 1
        Port from 16414 to 16418 -> 1
    Returns:
        int: instance_id, or None if failed to predict
    """
    try:
        port = int(serial.split(':')[1])
    except (IndexError, ValueError):
        return None
    index, offset = divmod(port - 16384 + 16, 32)
    offset -= 16
    if 0 <= index < 32 and offset in [-2, -1, 0, 1, 2]:
        return index
    else:
        return None