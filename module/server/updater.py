import datetime
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
import requests
from filelock import FileLock, Timeout
from contextlib import contextmanager
from typing import Generator, List, Tuple

from deploy.config import ExecutionError
from deploy.git import GitManager
from deploy.pip import PipManager
from deploy.utils import DEPLOY_CONFIG
from module.logger import logger
from module.base.retry import retry
from module.server.config import DeployConfig


# git 自动升级：内置 git 不可用且本机也找不到可用 git 时，下载完整版 git 替换 toolkit/Git。
# 必须用完整版（.tar.bz2，Python 原生可解）：MinGit 裁剪掉了 git-remote-http.exe，
# 实测无法通过 https 拉取远程，装了也没用。
GIT_MIN_VERSION = (2, 30, 0)
GIT_UPGRADE_VERSION = '2.55.0.3'

# repository / branch 校验。
#
# 这两个值会被**未加引号**拼进 shell=True 的 git 命令（见 execute_stream 与
# ensure_origin 里的 f'"{self.git}" remote set-url origin {repository}'），
# 所以真正的威胁面是 shell 元字符与参数注入，不是「地址长得像不像 GitHub」。
#
# 因此这里改成「拒绝危险字符 + 校验结构」，而不是枚举合法形态的白名单：
# 白名单挡掉了大量真实合法的地址——带 PAT 的 https://ghp_xxx@github.com/o/r.git、
# 带用户名密码的 https://user:pass@host/o/r.git、ssh://git@host:22/o/r.git、
# 以及主机名带下划线的自建 GitLab（https://gitlab.my_corp.com/...）。
# 用户改不了这些地址就等于用不了私有仓库，而它们对 shell 并不比公开地址危险。
#
# cmd.exe 的元字符（& | ; < > ^ ( ) % ! 换行 回车 制表 引号 反引号 $）一律拒绝；
# 空格也拒绝——未加引号时空格会把一个参数拆成两个。
# Git revision 通配符（* ? [ ]）同样拒绝；单引号、逗号、花括号在 cmd.exe
# 参数中没有特殊语义，保留给合法 branch 使用。
_SHELL_UNSAFE = set(' \t\r\n"`$&|;<>^();!*?[]')

# 允许的协议。git 支持的远程协议远不止这些，但 OAS 的更新流程只用得到
# http(s) 与 ssh；file:// 与 ext:: 之类能读写本地任意路径或执行外部命令，不放开。
_REPOSITORY_SCHEMES = ('http://', 'https://', 'ssh://', 'git://')
# scp 式短地址：git@host:owner/repo.git（无协议前缀，git 的默认形态）
_REPOSITORY_SCP_RE = re.compile(r'^[A-Za-z0-9._~-]+@[A-Za-z0-9._-]+:[^:]+$')


def _reject_shell_unsafe(value: str, field: str) -> None:
    """拒绝会在 shell=True 下改变命令语义的字符。"""
    bad = set(_SHELL_UNSAFE & set(value))
    # URL 编码中的 `%40` / `%2F` 是合法凭据或路径；先剥掉全部 `%HH`，
    # 剩下的 `%` 才可能是 `%VAR%` / `%%` 等 cmd.exe 展开语法。
    if '%' in re.sub(r'%[0-9A-Fa-f]{2}', '', value):
        bad.add('%')
    if bad:
        raise ValueError(f'{field} 含非法字符：{"".join(sorted(bad))!r}')


def validate_repository(repository: str) -> str:
    """校验仓库地址：拒绝 shell 元字符与不支持的协议，不限制主机与凭据形态。

    刻意接受这些真实合法的写法（曾被白名单误拒）：
        https://ghp_TOKEN@github.com/owner/repo.git   PAT
        https://user:pass@host/owner/repo.git         用户名密码
        ssh://git@github.com:22/owner/repo.git        显式 ssh 协议与端口
        https://gitlab.my_corp.com/owner/repo.git     主机名含下划线的自建服务
        git@github.com:owner/repo.git                 scp 式短地址
    """
    repository = str(repository).strip()
    if not repository:
        raise ValueError('repository 不能为空')
    if len(repository) > 2048:
        raise ValueError('repository 过长')
    _reject_shell_unsafe(repository, 'repository')
    # 前导 '-' 会被 git 当成选项而非地址（参数注入）
    if repository.startswith('-'):
        raise ValueError('repository 不能以 - 开头')
    lowered = repository.lower()
    if lowered.startswith(_REPOSITORY_SCHEMES):
        # 协议后必须真有主机名
        rest = repository.split('://', 1)[1]
        if not rest or rest.startswith('/'):
            raise ValueError('repository 缺少主机名')
        return repository
    if _REPOSITORY_SCP_RE.fullmatch(repository):
        return repository
    raise ValueError('repository 必须是 http(s)/ssh/git 协议或 git@host:path 形式')


# Git 明令禁止的 ref 形态（git check-ref-format）。这里只保留真正的禁止项，
# 不限制字符集——分支名允许非 ASCII，`_dev`、`修复/登录` 都是合法分支。
_BRANCH_FORBIDDEN = ('..', '//', '@{', '\\', '~', ':')


def validate_branch(branch: str) -> str:
    """校验 Git 分支名：拒绝 shell 元字符与 Git 禁止的 ref 形态。

    刻意接受下划线开头（`_dev`）与非 ASCII 分支名（`修复/登录`）——
    两者都是 git check-ref-format 允许的，此前的 `^[A-Za-z0-9]` 白名单把它们误拒了。
    """
    branch = str(branch).strip()
    if not branch:
        raise ValueError('branch 不能为空')
    if len(branch) > 255:
        raise ValueError('branch 过长')
    _reject_shell_unsafe(branch, 'branch')
    # 前导 '-' 会被 git 当成选项（如 --force），属参数注入
    if branch.startswith('-'):
        raise ValueError('branch 不能以 - 开头')
    for token in _BRANCH_FORBIDDEN:
        if token in branch:
            raise ValueError(f'branch 不能包含 {token!r}')
    # Git 禁止：以 / 或 . 开头、以 / . 结尾、以 .lock 结尾、路径段以 . 开头
    if (branch.startswith(('/', '.')) or branch.endswith(('/', '.'))
            or branch.lower().endswith('.lock') or '/.' in branch
            or branch == '@'):
        raise ValueError('branch 不是合法的 Git ref')
    # 控制字符与 DEL 同样被 git 拒绝
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in branch):
        raise ValueError('branch 含控制字符')
    return branch


def update_lock(file=DEPLOY_CONFIG, timeout: float = 0):
    """返回 OAS 更新/仓库操作共用的跨进程文件锁。

    默认 timeout=0（立即失败）只适合只读查询。真实更新与写配置必须传
    非零超时，见 UPDATE_LOCK_WAIT 的说明。
    """
    return FileLock(f'{os.path.abspath(file)}.update.lock', timeout=timeout)


# 真实更新/写配置等待锁的秒数。
#
# 为什么不能用默认的 0：/update_info 与 deploy.update --info 也持这把锁，
# 而它们内部的 check_update() 含 git fetch（连不上 GitHub 时可拖到十几秒），
# 并非只读，暂时无法与写操作分锁。timeout=0 意味着用户在页面刚打开、
# fetch 还没结束时点「更新」必被立即拒绝，看起来就是按钮没反应。
#
# check_update 最多两次 fetch，每次执行层 timeout=25 秒；等待上限必须留出
# 两次失败重试的完整窗口，否则慢查询结束前点击更新仍会被误拒。
UPDATE_LOCK_WAIT = 60.0


# 国内源优先（淘宝 npmmirror / 华为云），GitHub 代理兜底，失败依次尝试下一个
GIT_UPGRADE_URLS = [
    'https://registry.npmmirror.com/-/binary/git-for-windows/v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
    'https://mirrors.huaweicloud.com/git-for-windows/v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
    'https://gh-proxy.com/https://github.com/git-for-windows/git/releases/download/'
    'v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
]


class UpdateProgress:
    """线程安全的更新进度状态，供手动更新接口与前端轮询使用。

    status: idle / running / done / failed / rejected
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.status = 'idle'
        self.step = ''
        self.branch = ''
        self.logs = []
        self.finished = False
        # 行输出监听器。独立更新器（deploy/update.py）注册它把进度实时打到控制台；
        # web 路径不注册，日志只留在内存 buffer 里供 /update_progress 轮询。
        self._listener = None

    def set_listener(self, listener) -> None:
        """注册行输出监听器，传 None 取消。"""
        with self._lock:
            self._listener = listener

    def _notify(self, line) -> None:
        """把一行进度转发给监听器。

        必须在锁外调用：监听器是外部回调，在锁内执行会让它有机会造成死锁。
        监听器自身异常不得影响更新流程，因此整体吞掉。
        """
        listener = self._listener
        if listener is None:
            return
        try:
            listener(line)
        except Exception:
            pass

    def try_start(self, branch='') -> bool:
        """在进程内原子领取更新执行权，避免检查与启动线程之间的竞态。"""
        with self._lock:
            if self.status == 'running':
                return False
            self.status = 'running'
            self.step = ''
            self.branch = branch
            self.logs = []
            self.finished = False
            return True

    def reset(self, branch):
        with self._lock:
            self.status = 'running'
            self.step = ''
            self.branch = branch
            self.logs = []
            self.finished = False

    def append(self, line):
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]
        self._notify(line)

    def set_step(self, step):
        # 在锁内直接写 logs，避免嵌套加锁
        with self._lock:
            self.step = step
            self.logs.append(f'> {step}')
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]
        self._notify(f'> {step}')

    def finish(self, ok):
        # 在锁内直接写 logs，避免嵌套加锁（threading.Lock 不可重入）
        with self._lock:
            self.finished = True
            self.status = 'done' if ok else 'failed'
            self.logs.append('更新完成' if ok else '更新失败')
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]
        self._notify('更新完成' if ok else '更新失败')

    def reject(self, reason):
        # 在锁内直接写 logs，避免嵌套加锁
        with self._lock:
            self.finished = True
            self.status = 'rejected'
            self.logs.append(reason)
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]
        self._notify(reason)

    def snapshot(self):
        with self._lock:
            return {
                'status': self.status,
                'step': self.step,
                'branch': self.branch,
                'logs': list(self.logs),
                'finished': self.finished,
            }


_update_progress = UpdateProgress()


class Updater(DeployConfig, GitManager, PipManager):
    def __setattr__(self, key, value):
        # 独立更新器也复用 Updater，校验放在配置赋值层才能覆盖所有入口。
        if key == 'Repository' and value is not None:
            value = validate_repository(value)
        elif key == 'Branch' and value is not None:
            value = validate_branch(value)
        super().__setattr__(key, value)

    def __init__(self, file=DEPLOY_CONFIG):
        super().__init__(file=file)
        self.state = 0
        # fetch 成功与否，由 check_update 记录，供 /update_info 透出以区分「检查失败」与「无更新」
        self.fetch_ok = False

    @property
    def delay(self):
        self.read()
        return int(self.CheckUpdateInterval) * 60

    @property
    def schedule_time(self):
        self.read()
        t = self.AutoRestartTime
        if t is not None:
            return datetime.time.fromisoformat(t)
        else:
            return None

    def execute_output(self, command) -> str:
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            shell=True,
            creationflags=flags,
        )
        out, _ = proc.communicate()
        return out or ''

    def execute_stream(self, command, on_line=None) -> bool:
        """
        静默执行命令并逐行回调输出，返回进程是否成功（退出码 0）。

        不使用 os.system / subprocess.run(shell=True)：GUI(pythonw) 无控制台时
        会给 cmd 弹窗，CREATE_NO_WINDOW 让子进程无窗口静默运行。
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            shell=True,
            creationflags=flags,
        )
        for line in proc.stdout:
            line = line.rstrip('\n')
            logger.info(line)
            if on_line:
                on_line(line)
        return proc.wait() == 0

    @property
    def git_root(self) -> str:
        """GitExecutable 所在 git 安装根目录（绝对路径）。

        例如 toolkit/Git/mingw64/bin/git.exe -> .../toolkit/Git
        """
        if hasattr(self, '_git_root'):
            return self._git_root
        exe = self.git
        return os.path.dirname(os.path.dirname(os.path.dirname(exe)))

    @git_root.setter
    def git_root(self, value):
        # 允许注入/覆盖（测试或运行时指定 git 根目录）
        self._git_root = value

    @property
    def git_is_builtin(self) -> bool:
        """GitExecutable 是否指向项目内置 toolkit/Git（只有内置 git 才能自动升级）。"""
        if hasattr(self, '_git_is_builtin'):
            return self._git_is_builtin
        toolkit_dir = os.path.abspath('./toolkit').replace('\\', '/')
        return str(self.git_root).replace('\\', '/').startswith(toolkit_dir)

    @git_is_builtin.setter
    def git_is_builtin(self, value):
        self._git_is_builtin = value

    def _git_core_dir(self, exe) -> str:
        """定位 git 的 git-core 组件目录。

        优先问 git 自己（`--exec-path`，对 mingw64/bin 与 cmd 两种布局都准确），
        取不到有效目录时回退到按 git_root 推导。
        """
        try:
            exec_path = self.execute_output(f'"{exe}" --exec-path').strip()
        except Exception:
            exec_path = ''
        if exec_path and os.path.isdir(exec_path):
            return exec_path
        return os.path.join(self.git_root, 'mingw64', 'libexec', 'git-core')

    def _probe_git(self, exe):
        """
        离线检查指定 git.exe 是否具备 https 拉取能力。

        Args:
            exe: git.exe 路径

        Returns:
            tuple: (可用 bool, 原因 str)
        """
        try:
            version_text = self.execute_output(f'"{exe}" --version').strip()
        except Exception as e:
            return False, f'git 无法执行: {e}'
        m = re.search(r'(\d+)\.(\d+)\.(\d+)', version_text)
        if not m:
            return False, f'无法解析 git 版本: {version_text}'
        version = tuple(int(x) for x in m.groups())
        if version < GIT_MIN_VERSION:
            return False, f'git 版本过旧 ({version_text.strip()})，与 GitHub 协议不兼容'
        # 检查 https 传输组件，缺则必然无法拉取远程（MinGit 就缺这个）
        remote_http = os.path.join(self._git_core_dir(exe), 'git-remote-http.exe')
        if not os.path.exists(remote_http):
            return False, f'git 缺少 git-remote-http.exe，无法通过 https 拉取远程'
        return True, ''

    def check_git_usable(self):
        """
        离线检查当前 GitExecutable 是否具备 https 拉取能力。

        Returns:
            tuple: (可用 bool, 原因 str)
        """
        return self._probe_git(self.git)

    def use_git(self, exe) -> None:
        """
        切换 GitExecutable 到指定 git.exe 并落盘 deploy.yaml。

        `GitManager.git` 是 cached_property，改配置后必须失效缓存，
        否则同一实例仍会用切换前的旧路径。
        """
        self.GitExecutable = str(exe).replace('\\', '/')
        self.__dict__.pop('git', None)
        self.__dict__.pop('_git_root', None)

    def ensure_origin(self) -> bool:
        """确保 git origin 与 deploy.yaml 的 Repository 一致，不一致时自动切换。"""
        try:
            repository = validate_repository(self.Repository)
            validate_branch(self.Branch)
        except ValueError as e:
            logger.error(f'更新配置校验失败：{e}')
            return False
        current = self.execute_output(f'"{self.git}" remote get-url origin').strip()
        if current == self.Repository:
            return True
        if current.startswith(('http://', 'https://', 'git@')):
            # origin 存在但地址不同，set-url 覆盖
            ok = self.execute_stream(f'"{self.git}" remote set-url origin {repository}')
        else:
            # origin 不存在（get-url 输出报错文本），用 add 创建
            ok = self.execute_stream(f'"{self.git}" remote add origin {repository}')
        if ok:
            logger.info(f'origin 已同步到 {repository}')
            return True
        logger.warning('同步 origin 失败，更新可能仍走旧远程')
        return False

    def find_usable_git(self):
        """
        探测本机已安装的可用 git（含 https 传输组件），零下载优先。

        依次尝试 PATH 中的 git 与常见安装路径，返回第一个可用的 git.exe 路径，
        全部不可用时返回 None。

        Returns:
            str | None
        """
        candidates = []
        which = shutil.which('git')
        if which:
            candidates.append(which)
        for base in (
                os.environ.get('ProgramFiles', r'C:\Program Files'),
                os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                os.environ.get('LOCALAPPDATA', ''),
        ):
            if base:
                candidates.append(os.path.join(base, 'Git', 'cmd', 'git.exe'))
                candidates.append(os.path.join(base, 'Programs', 'Git', 'cmd', 'git.exe'))

        current = os.path.abspath(self.git).replace('\\', '/').lower()
        seen = set()
        for exe in candidates:
            exe = os.path.abspath(exe)
            key = exe.replace('\\', '/').lower()
            # 跳过重复候选与当前已判定不可用的 GitExecutable
            if key in seen or key == current:
                continue
            seen.add(key)
            if not os.path.exists(exe):
                continue
            usable, _ = self._probe_git(exe)
            if usable:
                return exe
        return None

    def upgrade_git(self, on_line=None) -> bool:
        """
        下载完整版 git 替换 toolkit/Git，失败自动回滚。返回是否成功。

        仅在内置 git 不可用、且本机也找不到可用 git 时作为兜底调用。

        Args:
            on_line: 进度回调（写入更新 log）
        """
        def emit(msg):
            if on_line:
                on_line(msg)
            else:
                logger.info(msg)

        if not self.git_is_builtin:
            emit('GitExecutable 不指向内置 toolkit/Git，无法自动升级，请手动配置可用 git')
            return False

        tmp_dir = tempfile.mkdtemp(prefix='oas_git_upgrade_')
        # 归档名沿用下载 URL 的后缀，_extract_archive 按后缀选解压方式
        archive_name = GIT_UPGRADE_URLS[0].rsplit('/', 1)[-1]
        zip_path = os.path.join(tmp_dir, archive_name)
        extract_dir = os.path.join(tmp_dir, 'git_new')
        try:
            # 1. 下载（多源依次尝试），进度节流后写入 log
            downloaded = False
            for url in GIT_UPGRADE_URLS:
                emit(f'下载完整版 git {GIT_UPGRADE_VERSION}（约 116MB，视网速可能需要几分钟）…')
                last_reported = 0

                def progress(d, t):
                    # 有总大小按 10% 一档、无总大小按每 10MB 一档，避免逐块刷屏
                    nonlocal last_reported
                    if t:
                        pct = d * 100 // t
                        if pct >= last_reported + 10 or d >= t:
                            last_reported = pct
                            emit(f'下载中: {d / 1048576:.1f} MB / {t / 1048576:.1f} MB ({pct}%)')
                    else:
                        mb = d // (10 * 1048576)
                        if mb > last_reported:
                            last_reported = mb
                            emit(f'下载中: {d / 1048576:.1f} MB')

                try:
                    self._download_git_archive(url, zip_path, on_progress=progress)
                    downloaded = True
                    break
                except Exception as e:
                    emit(f'下载失败({url}): {e}')
            if not downloaded:
                emit('所有下载源均失败')
                return False
            emit('下载完成')

            # 2. 解压（校验路径，防归档路径穿越）
            os.makedirs(extract_dir, exist_ok=True)
            emit('解压中（完整版体积较大，请耐心等待）…')
            self._extract_archive(zip_path, extract_dir)
            if not os.path.exists(os.path.join(extract_dir, 'mingw64', 'bin', 'git.exe')):
                emit('解压内容缺少 mingw64/bin/git.exe，结构异常')
                return False
            # 必须含 https 传输组件，否则替换了也拉不了远程（MinGit 就是这样）
            if not os.path.exists(os.path.join(
                    extract_dir, 'mingw64', 'libexec', 'git-core', 'git-remote-http.exe')):
                emit('解压内容缺少 git-remote-http.exe，该 git 无法通过 https 拉取，放弃替换')
                return False
            emit('解压完成')

            # 3. 备份 -> 替换 -> 验证，失败回滚
            git_root = self.git_root
            backup = git_root + '.bak'
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            # 原 git 目录可能已被删除（如用户手动删掉 toolkit/Git 后走下载），此时无需备份
            if os.path.exists(git_root):
                shutil.move(git_root, backup)
            try:
                shutil.move(extract_dir, git_root)
            except Exception as e:
                self._restore_git(git_root, backup)
                emit(f'替换失败并已回滚: {e}')
                return False
            try:
                version_text = self.execute_output(f'"{self.git}" --version').strip()
                if not version_text:
                    raise Exception('git --version 无输出')
            except Exception as e:
                self._restore_git(git_root, backup)
                emit(f'新 git 验证失败并已回滚: {e}')
                return False
            # 验证通过，清理备份
            shutil.rmtree(backup, ignore_errors=True)
            emit(f'git 已升级到 {version_text}')
            return True
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _extract_archive(path, dest) -> None:
        """解压下载的 git 归档到 dest，拒绝含路径穿越的成员。

        完整版 git 发行包是 .tar.bz2（Python 原生可解，无需外部 7z）；
        同时兼容 .zip，便于切换下载源。
        """
        def unsafe(name):
            return ('..' in name.replace('\\', '/').split('/')
                    or name.startswith('/')
                    or (len(name) > 1 and name[1] == ':'))

        if path.endswith('.zip'):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if unsafe(info.filename):
                        raise Exception(f'非法归档路径: {info.filename}')
                zf.extractall(dest)
            return
        with tarfile.open(path, 'r:bz2') as tf:
            for member in tf.getmembers():
                if unsafe(member.name):
                    raise Exception(f'非法归档路径: {member.name}')
            tf.extractall(dest)

    @staticmethod
    def _restore_git(git_root, backup):
        """替换失败时从备份恢复 toolkit/Git。"""
        if os.path.exists(git_root):
            shutil.rmtree(git_root, ignore_errors=True)
        if os.path.exists(backup):
            shutil.move(backup, git_root)

    def _download_git_archive(self, url, dest, on_progress=None) -> None:
        """流式下载 git 归档到 dest，失败抛异常。

        Args:
            on_progress: 进度回调(downloaded_bytes, total_bytes)，每个数据块调用一次
        """
        with requests.get(url, stream=True, timeout=(15, 180)) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length') or 0)
            downloaded = 0
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
        if total and downloaded < total:
            raise Exception(f'下载不完整: {downloaded}/{total} 字节')

    def get_commit(self, revision="", n=1, short_sha1=False) -> Tuple:
        """
        Return:
            (sha1, author, isotime, message,)
        """
        ph = "h" if short_sha1 else "H"

        log = self.execute_output(
            f'"{self.git}" log {revision} --pretty=format:"%{ph}---%an---%ad---%s" --date=iso -{n}'
        )

        if not log:
            return None, None, None, None

        # git 失败时 fatal 报错文本（stderr 被合并进 stdout）可能混入输出，且 commit
        # message 含换行时也会被拆成多行；只保留标准 4 字段行，避免把报错当 commit 返回
        logs = [
            tuple(line.split("---"))
            for line in log.split("\n")
            if len(line.split("---")) == 4
        ]
        if not logs:
            return None, None, None, None

        if n == 1:
            return logs[0]
        else:
            return logs

    def current_branch(self) -> str:
        return self.Branch

    def current_commit(self) -> str:
        return self.get_commit()

    def latest_commit(self) -> str:
        source = "origin"
        return self.get_commit(f"{source}/{self.Branch}")

    def check_update(self) -> bool:
        self.state = "checking"

        # if State.deploy_config.GitOverCdn:
        #     status = self.goc_client.get_status()
        #     if status == "uptodate":
        #         logger.info(f"No update")
        #         return False
        #     elif status == "behind":
        #         logger.info(f"New update available")
        #         return True
        #     else:
        #         # failed, should fallback to `git pull`
        #         pass

        # 确保 fetch 源与 deploy.yaml Repository 一致（用户直接改 yaml 时自动换源）
        self.fetch_ok = False
        if not self.ensure_origin():
            return False

        source = "origin"
        # fetch 加快速失败参数（TCP 3s、慢速 10s 中止），进程 25s 兜底 kill：
        # 连不上远程时尽快失败返回，避免 update_info 长时间挂起阻塞更新器页
        # fetch_ok 记录 fetch 是否成功，供 /update_info 区分「检查失败」与「无更新」
        self.fetch_ok = False
        for _ in range(2):
            if self.execute(
                    f'"{self.git}" -c http.connectTimeout=3 -c http.lowSpeedLimit=1000 '
                    f'-c http.lowSpeedTime=10 fetch {source} {self.Branch}',
                    allow_failure=True,
                    timeout=25,
            ):
                self.fetch_ok = True
                break
        else:
            logger.warning("Git fetch failed")
            return False

        log = self.execute_output(
            f'"{self.git}" log --not --remotes={source}/* -1 --oneline'
        )
        if log:
            if self.KeepLocalChanges:
                logger.info(
                    f"Cannot find local commit {log.split()[0]} in upstream, skip update"
                )
                return False
            # 安装版以远端分支为准；实际更新时会强制丢弃阻塞同步的本地提交。
            logger.warning(
                f"Local commit {log.split()[0]} is not in upstream, force update is allowed"
            )

        sha1, _, _, message = self.get_commit(f"..{source}/{self.Branch}")

        if sha1:
            logger.info(f"New update available")
            logger.info(f"{sha1[:8]} - {message}")
            return True
        else:
            logger.info(f"No update")
            return False

    def execute_pull(self, before_ocr=None) -> bool:
        """在跨进程更新锁内执行完整更新，避免 OASX 与 Web 同时改 Git。"""
        try:
            # 等一个非零超时：只读查询（/update_info）也持这把锁且内含 git fetch，
            # 立即失败会让「页面刚打开就点更新」必被拒。见 UPDATE_LOCK_WAIT。
            with update_lock(self.file, timeout=UPDATE_LOCK_WAIT):
                return self._execute_pull_locked(before_ocr=before_ocr)
        except Timeout:
            _update_progress.reject('已有其它 OAS 更新进程正在运行，拒绝并发更新')
            logger.warning(f'Update lock is busy: {os.path.abspath(self.file)}.update.lock')
            return False

    def _execute_pull_locked(self, before_ocr=None) -> bool:
        """拉取远端代码并对齐 OCR 依赖。幂等，中断后重跑会接上剩余阶段。

        Args:
            before_ocr: 可选钩子，签名 before_ocr(prog) -> bool，在代码更新完成、
                OCR 对齐之前调用。独立更新器（deploy/update.py）用它插入 pip 依赖
                对齐；web 路径不传，行为与此前完全一致。

        Returns:
            bool: 是否全流程成功。
        """
        source = 'origin'
        prog = _update_progress
        prog.reset(branch=self.Branch)
        # 每个阶段用 set_step 标记，失败点在 finish(False) 前用 append 写明「阶段 + 原因」，
        # 让 /update_progress 的 logs 能直接看出更新中断在哪一步。
        prog.set_step('开始更新')

        # 0. 检查 git 可用性；不可用时先找本机已装 git（零下载），再退到自动升级
        usable, reason = self.check_git_usable()
        if not usable:
            prog.set_step(f'git 不可用：{reason}')
            found = self.find_usable_git()
            if found:
                # 本机已有可用 git，改写 GitExecutable（DeployConfig 会落盘 deploy.yaml）
                prog.append(f'发现本机可用 git：{found}，切换使用')
                self.use_git(found)
                usable, reason = self.check_git_usable()
            if not usable:
                prog.set_step('未发现可用 git，尝试自动下载升级')
                if not self.upgrade_git(on_line=prog.append):
                    prog.reject(f'git 自动升级失败，无法继续更新：{reason}')
                    return False
                usable, reason = self.check_git_usable()
                if not usable:
                    prog.reject(f'git 升级后仍不可用：{reason}')
                    return False

        # 0.4 清理强杀 git 后残留的 .git/*.lock，否则 fetch/merge 会直接报锁已存在。
        #     依赖 /execute_update 的单实例防重入：只有当前更新线程在跑 git，删除才是安全的。
        prog.set_step('清理残留的 git 锁')
        self.cleanup_git_locks()

        # 0.5 确保 fetch 源与 deploy.yaml Repository 一致（用户直接改 yaml 时自动换源）
        prog.set_step('同步远程源 origin')
        if not self.ensure_origin():
            prog.reject('同步 origin 失败，拒绝使用旧远程更新')
            return False

        # 1. fetch 目标分支（加快速失败参数，连不上远程时尽快失败，不让后台线程空挂）
        prog.set_step(f'fetch {source}/{self.Branch}')
        fetched = False
        for _ in range(3):
            if self.execute_stream(
                    f'"{self.git}" -c http.connectTimeout=3 -c http.lowSpeedLimit=1000 '
                    f'-c http.lowSpeedTime=10 fetch {source} {self.Branch}',
                    on_line=prog.append
            ):
                fetched = True
                break
        if not fetched:
            prog.append('阶段「拉取远程代码」失败：连不上远程仓库或网络超时（已重试 3 次），上面是 git fetch 的报错输出')
            prog.finish(False)
            logger.warning('Git fetch failed')
            return False

        # 2. 若当前分支不是目标分支，先切换再更新
        current = self.execute_output(f'"{self.git}" symbolic-ref --short HEAD').strip()
        if current != self.Branch:
            prog.set_step(f'switch branch: {current} -> {self.Branch}')
            # 保留本地改动时拒绝跨分支；安装版则允许继续执行强制同步。
            unpushed = self.execute_output(
                f'"{self.git}" log --not --remotes={source}/* -1 --oneline').strip()
            if unpushed:
                if self.KeepLocalChanges:
                    prog.reject(f'本地存在未推送的提交：{unpushed}，拒绝切换分支。请先 push。')
                    return False
                prog.append(f'发现未推送提交：{unpushed}，继续按远端分支强制更新')
            # 已跟踪文件有改动时，保留模式拒绝切换，安装模式才直接丢弃。
            if not self.execute_stream(f'"{self.git}" diff --quiet HEAD'):
                if self.KeepLocalChanges:
                    prog.reject('工作区有已跟踪文件的修改，已按 KeepLocalChanges 配置停止更新。')
                    return False
                prog.append('工作区有已跟踪文件的修改，直接丢弃后切换')
                if not self.execute_stream(f'"{self.git}" reset --hard HEAD', on_line=prog.append):
                    prog.append('阶段「切换分支」失败：丢弃本地改动失败（git reset --hard）')
                    prog.finish(False)
                    logger.warning('Git reset --hard failed')
                    return False
            # 清理未跟踪文件/目录（忽略 .gitignore 保护项），
            # 避免与目标分支同名文件冲突导致 checkout 被覆盖拦截
            if not self.KeepLocalChanges:
                prog.append('清理未跟踪文件，确保切换不被覆盖冲突拦截')
                if not self.execute_stream(f'"{self.git}" clean -fd', on_line=prog.append):
                    prog.append('阶段「切换分支」失败：清理未跟踪文件失败（git clean -fd）')
                    prog.finish(False)
                    logger.warning('Git clean failed')
                    return False
            # 切换：本地已有则直接 checkout，否则基于远程创建跟踪分支
            if self.execute_stream(f'"{self.git}" show-ref --verify refs/heads/{self.Branch}'):
                prog.set_step(f'git checkout {self.Branch}')
                switched = self.execute_stream(
                    f'"{self.git}" checkout {self.Branch}', on_line=prog.append
                )
            else:
                prog.set_step(f'git checkout -b {self.Branch} {source}/{self.Branch}')
                switched = self.execute_stream(
                    f'"{self.git}" checkout -b {self.Branch} {source}/{self.Branch}',
                    on_line=prog.append,
                )
            if not switched:
                prog.append('阶段「切换分支」失败：git checkout 退出码非 0，上面是 git 的报错输出')
                prog.finish(False)
                logger.warning('Git checkout failed')
                return False

        # 3. fetch 已完成，直接从远端跟踪分支快进，避免 pull 再次联网或生成合并提交。
        prog.set_step(f'fast-forward {source}/{self.Branch}')
        fast_forwarded = self.execute_stream(
            f'"{self.git}" merge --ff-only {source}/{self.Branch}',
            on_line=prog.append,
        )
        if not fast_forwarded:
            if self.KeepLocalChanges:
                prog.append('阶段「快进合并」失败：本地改动与远端分叉，且配置了 KeepLocalChanges 保留本地改动，拒绝强制同步')
                prog.finish(False)
                logger.warning('Git fast-forward failed; local changes are preserved')
                return False

            # 安装版默认不保留源码改动：清理非忽略文件后，以已 fetch 的远端提交覆盖本地状态。
            # git clean 不带 -x，因此受 .gitignore 保护的未跟踪运行数据不会被删除。
            prog.set_step(f'force sync {source}/{self.Branch}')
            prog.append('快进被本地改动或分叉阻塞，丢弃源码改动并强制同步远端')
            if not self.execute_stream(f'"{self.git}" clean -fd', on_line=prog.append):
                prog.append('阶段「强制同步」失败：清理未跟踪文件失败（git clean -fd）')
                prog.finish(False)
                logger.warning('Git clean failed during force sync')
                return False
            if not self.execute_stream(
                    f'"{self.git}" reset --hard {source}/{self.Branch}',
                    on_line=prog.append,
            ):
                prog.append('阶段「强制同步」失败：丢弃本地改动失败（git reset --hard）')
                prog.finish(False)
                logger.warning('Git reset --hard failed during force sync')
                return False

        # 4. 代码已是最新。先按需对齐 pip 依赖（新代码可能引入新 requirements，
        #    且 requirements 变动会牵动 ORT 的间接依赖），顺序与 deploy.installer 一致。
        if before_ocr is not None:
            if not before_ocr(prog):
                prog.finish(False)
                return False

        # 5. 紧接着对齐 OCR 依赖与模型，让「一键更新」真正一键到位。
        #    finish(True) 必须放在 OCR 对齐之后：此前在 git 拉完就置 done，UI 显示
        #    "更新完成"时 OCR 依赖可能尚未对齐，对齐失败也会被"更新完成"掩盖，
        #    曾导致用户以为更新成功、实跑任务才发现 OCR 全挂。
        if not self.align_ocr(prog):
            # align_ocr 内部已把失败原因写入 logs（含"停止实例后重试"提示）
            prog.finish(False)
            return False

        prog.finish(True)
        logger.info('Update finished')
        return True

    def cleanup_git_locks(self) -> None:
        """清理强杀 git 后残留在 .git 下的 *.lock，避免 fetch/merge 报锁已存在。

        前提是没有并发 git 进程：调用方 execute_pull 由 /execute_update 的单实例
        防重入保证同一时刻只有一个更新线程，因此这里删除 lock 是安全的。

        用 rev-parse 定位真实 git 目录（兼容 .git 文件/子目录形式），再递归删除。
        """
        git_dir = self.execute_output(f'"{self.git}" rev-parse --git-dir').strip()
        if not git_dir:
            return
        git_dir = os.path.abspath(git_dir)
        if not os.path.isdir(git_dir):
            return
        removed = 0
        for root, _, files in os.walk(git_dir):
            for name in files:
                if not name.endswith('.lock'):
                    continue
                path = os.path.join(root, name)
                try:
                    os.remove(path)
                    removed += 1
                except OSError as e:
                    logger.warning(f'Failed to remove stale git lock {path}: {e}')
        if removed:
            logger.info(f'Removed {removed} stale git lock(s) under {git_dir}')

    def align_ocr(self, prog=None) -> bool:
        """更新后对齐 OCR 依赖；只在确需换包时停止 RPC，并保证失败恢复。"""
        emit = prog.append if prog else logger.info
        if not self.OcrAutoAlignDeps:
            emit('OcrAutoAlignDeps 关闭，跳过 OCR 依赖对齐')
            return True

        rpc_stopped = False
        success = False
        ensure_ocr_server_started = None
        try:
            from deploy.ocr_deps import OcrDepsManager
            from module.ocr.rpc import (ensure_ocr_server_started,
                                        kill_orphan_ocr_servers,
                                        shutdown_ocr_server)

            manager = OcrDepsManager(file=self.file)
            # 只读检查和本进程 DLL 守卫必须先做，避免“无需换包”或注定失败时
            # 先把正常运行的共享 OCR 服务停掉。
            if manager.check():
                emit('OCR 依赖已是 PP-OCRv6，无需变更')
                if self.StartOcrServer:
                    if ensure_ocr_server_started():
                        emit('OCR RPC 服务已确认运行')
                    else:
                        emit('OCR RPC 服务启动失败，可稍后重启 OAS 恢复')
                        return False
                success = True
                return True

            if 'onnxruntime' in sys.modules:
                emit('OCR 依赖需要换包，但当前进程已加载 onnxruntime，Windows 锁定 DLL 会让换包失败，已跳过。')
                emit('请关闭 OAS（含 GUI 与所有实例）后双击 oas-update.bat，在干净进程里完成更新与 OCR 对齐。')
                return False

            # 只有真正需要换包且当前进程没有加载 ORT 时才释放服务进程。
            rpc_stopped = bool(shutdown_ocr_server())
            orphan_result = kill_orphan_ocr_servers()
            if orphan_result < 0:
                emit('无法确认外部 OCR 服务已退出，已中止换包以避免损坏 onnxruntime。')
                return False
            rpc_stopped = rpc_stopped or orphan_result > 0
            time.sleep(0.5)

            if prog:
                prog.set_step('对齐 OCR 依赖（PP-OCRv6）')
            ok = self.execute_stream(
                f'"{manager.python}" -m deploy.ocr_deps',
                on_line=emit,
            )
            if not ok:
                emit('OCR 依赖对齐失败，可能原因与处理建议：')
                emit(self._diagnose_ocr_blockers())
                return False
            if self.StartOcrServer:
                if not ensure_ocr_server_started():
                    emit('OCR RPC 服务恢复失败，更新未完成，请稍后重启 OAS')
                    return False
                emit('OCR RPC 服务已恢复')
                # 已经在成功路径恢复，finally 不再重复拉起。
                rpc_stopped = False
            success = True
            return True
        except Exception as e:
            logger.exception(e)
            emit(f'OCR 依赖对齐异常：{e}')
            return False
        finally:
            # 清理成功或失败后都恢复曾由本流程停止的共享 OCR 服务。
            if (rpc_stopped and self.StartOcrServer
                    and ensure_ocr_server_started is not None):
                try:
                    if ensure_ocr_server_started():
                        emit('OCR 对齐失败，已恢复原 RPC 服务')
                    else:
                        emit('OCR RPC 服务恢复失败，请稍后重启 OAS')
                except Exception as e:
                    logger.exception(f'恢复 OCR RPC 服务失败：{e}')
                    emit(f'OCR 对齐失败，恢复 RPC 服务异常：{e}')

    def _diagnose_ocr_blockers(self) -> str:
        """OCR 对齐失败时定位可能占用 onnxruntime.dll 的进程，给出可操作提示。

        OCR 换包失败的常见根因是 Windows 锁定已加载的 onnxruntime.dll
        （表现为 WinError 5 拒绝访问），而更新器只能释放当前进程持有的 OCR 服务。
        这里枚举 OAS 安装目录下的 python/pythonw/oas 进程，把「谁还活着」指出来，
        用户停止这些进程后重试更新即可恢复。
        """
        try:
            from deploy.process import ProcessManager
            pm = ProcessManager(file=self.file)
        except Exception as e:
            return (f'无法枚举占用进程（{e}）。最常见原因是还有 OAS 实例/GUI/OCR 服务'
                    '在运行，请全部停止后重试更新。')
        hints = []
        enumerated = False
        for name in ('python.exe', 'pythonw.exe', 'oas.exe'):
            try:
                rows = pm.iter_process_by_name(name)
            except Exception:
                continue
            if not rows:
                continue
            enumerated = True
            for path, process_name, pid in rows:
                if pid != os.getpid():
                    hints.append(f'  {process_name} (PID {pid})：{path}')
        if hints:
            return ('检测到 OAS 相关进程仍在运行，可能持有 onnxruntime.dll 导致换包失败：\n'
                    + '\n'.join(hints)
                    + '\n请先全部停止上述进程，再重新执行更新。')
        if enumerated:
            return ('未检测到 OAS 相关进程，但 OCR 依赖仍对齐失败。'
                    '请检查是否有其它程序占用 toolkit\\lib\\site-packages\\onnxruntime，'
                    '或在任务管理器中结束所有 python 进程后重试更新。')
        return '无法枚举进程（可能缺少 pywin32），请先结束所有 OAS/GUI/OCR 进程后重试更新。'



if __name__ == "__main__":
    updater = Updater()
    print(updater.latest_commit())

