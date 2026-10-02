"""与管家共用的完整配置字节备份；调用方须持有配置事务锁。"""
import os
import tempfile
from pathlib import Path


def backup_source_config(source: Path) -> Path:
    """原子覆盖固定备份，失败向上传播，调用方不得继续改写配置。"""
    source = Path(source)
    directory = source.parent / '.butler_backups'
    directory.mkdir(exist_ok=True)
    destination = directory / source.name
    temporary = None
    try:
        # 直接复制字节，保留编码、BOM、缩进、换行及所有未识别字段。
        content = source.read_bytes()
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.backup-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return destination
    finally:
        # 替换成功后临时文件已消失，失败时清理残留且保留旧备份。
        if temporary is not None:
            temporary.unlink(missing_ok=True)
