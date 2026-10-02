# This Python file uses the following encoding: utf-8
# copy from alas https://github.com/LmeSzinc/AzurLaneAutoScript
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
import zipfile

import requests

from deploy.config import DeployConfig
from deploy.logger import logger
from deploy.utils import *


# 内置 toolkit git 过旧/缺组件无法 https 拉取时，安装阶段直接下载完整版替换。
# 必须用完整版（.tar.bz2，Python 原生可解）：MinGit 裁剪掉了 git-remote-http.exe，无法拉远程。
GIT_MIN_VERSION = (2, 30, 0)
GIT_FULL_VERSION = '2.55.0.3'
# 国内源优先（淘宝 npmmirror / 华为云），GitHub 代理兜底，失败依次尝试下一个
GIT_FULL_URLS = [
    'https://registry.npmmirror.com/-/binary/git-for-windows/v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
    'https://mirrors.huaweicloud.com/git-for-windows/v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
    'https://gh-proxy.com/https://github.com/git-for-windows/git/releases/download/'
    'v2.55.0.windows.3/Git-2.55.0.3-64-bit.tar.bz2',
]


class GitManager(DeployConfig):
    @cached_property
    def git(self):
        return self.filepath('GitExecutable')

    @staticmethod
    def remove(file):
        try:
            os.remove(file)
            logger.info(f'Removed file: {file}')
        except FileNotFoundError:
            logger.info(f'File not found: {file}')

    def git_repository_init(
            self, repo, source='origin', branch='master',
            proxy='', ssl_verify=True, keep_changes=False
    ):
        logger.hr('Git Init', 1)
        if not self.execute(f'"{self.git}" init', allow_failure=True):
            self.remove('./.git/config')
            self.remove('./.git/index')
            self.remove('./.git/HEAD')
            self.execute(f'"{self.git}" init')

        logger.hr('Set Git Proxy', 1)
        if proxy:
            self.execute(f'"{self.git}" config --local http.proxy {proxy}')
            self.execute(f'"{self.git}" config --local https.proxy {proxy}')
        else:
            self.execute(f'"{self.git}" config --local --unset http.proxy', allow_failure=True)
            self.execute(f'"{self.git}" config --local --unset https.proxy', allow_failure=True)

        if ssl_verify:
            self.execute(f'"{self.git}" config --local http.sslVerify true', allow_failure=True)
        else:
            self.execute(f'"{self.git}" config --local http.sslVerify false', allow_failure=True)

        logger.hr('Set Git Repository', 1)
        if not self.execute(f'"{self.git}" remote set-url {source} {repo}', allow_failure=True):
            self.execute(f'"{self.git}" remote add {source} {repo}')

        logger.hr('Fetch Repository Branch', 1)
        self.execute(f'"{self.git}" fetch {source} {branch}')

        logger.hr('Pull Repository Branch', 1)
        # Remove git lock
        for lock_file in [
            './.git/index.lock',
            './.git/HEAD.lock',
            './.git/refs/heads/master.lock',
        ]:
            if os.path.exists(lock_file):
                logger.info(f'Lock file {lock_file} exists, removing')
                os.remove(lock_file)
        if keep_changes:
            if self.execute(f'"{self.git}" stash', allow_failure=True):
                self.execute(f'"{self.git}" pull --ff-only {source} {branch}')
                if self.execute(f'"{self.git}" stash pop', allow_failure=True):
                    pass
                else:
                    # No local changes to existing files, untracked files not included
                    logger.info('Stash pop failed, there seems to be no local changes, skip instead')
            else:
                logger.info('Stash failed, this may be the first installation, drop changes instead')
                self.execute(f'"{self.git}" reset --hard {source}/{branch}')
                self.execute(f'"{self.git}" pull --ff-only {source} {branch}')
        else:
            self.execute(f'"{self.git}" reset --hard {source}/{branch}')
            self.execute(f'"{self.git}" pull --ff-only {source} {branch}')

        logger.hr('Show Version', 1)
        self.execute(f'"{self.git}" --no-pager log --no-merges -1')

    def git_install(self):
        logger.hr('Update Alas', 0)

        if not self.AutoUpdate:
            logger.info('AutoUpdate is disabled, skip')
            return

        self.git_repository_init(
            repo=self.Repository,
            source='origin',
            branch=self.Branch,
            proxy=self.GitProxy,
            ssl_verify=self.SSLVerify,
            keep_changes=self.KeepLocalChanges,
        )

    def ensure_git_ready(self, on_line=None) -> bool:
        """确保内置 toolkit/Git 可用：不可用则下载完整版替换。返回是否可用。

        在安装流程 git_install 之前调用，保证拉取代码用的内置 git 具备 https 能力。
        固定检查 ./toolkit/Git，不依赖 GitExecutable 配置指向。
        """
        def emit(msg):
            if on_line:
                on_line(msg)
            else:
                logger.info(msg)

        toolkit_root = os.path.abspath('./toolkit/Git').replace('\\', '/')
        git_exe = os.path.join(toolkit_root, 'mingw64', 'bin', 'git.exe')
        usable, reason = self._check_git_ready(git_exe, toolkit_root)
        if usable:
            return True
        emit(f'内置 git 不可用：{reason}，安装时自动下载完整版替换')
        if not self.download_git_full(toolkit_root, on_line=emit):
            return False
        usable, reason = self._check_git_ready(git_exe, toolkit_root)
        if not usable:
            emit(f'内置 git 替换后仍不可用：{reason}')
            return False
        return True

    @staticmethod
    def _check_git_ready(exe, git_root):
        """检查指定 git 是否具备 https 拉取能力。返回 (可用 bool, 原因 str)。

        直接对 exe 执行 --version 并校验 git-remote-http.exe，不依赖配置的 GitExecutable。
        """
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        try:
            proc = subprocess.Popen(
                f'"{exe}" --version',
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding='utf-8',
                errors='replace',
                shell=True,
                creationflags=flags,
            )
            version_text, _ = proc.communicate(timeout=15)
        except Exception as e:
            return False, f'git 无法执行: {e}'
        version_text = (version_text or '').strip()
        m = re.search(r'(\d+)\.(\d+)\.(\d+)', version_text)
        if not m:
            return False, f'无法解析 git 版本: {version_text}'
        version = tuple(int(x) for x in m.groups())
        if version < GIT_MIN_VERSION:
            return False, f'git 版本过旧 ({version_text})，与 GitHub 协议不兼容'
        remote_http = os.path.join(
            git_root, 'mingw64', 'libexec', 'git-core', 'git-remote-http.exe')
        if not os.path.exists(remote_http):
            return False, 'git 缺少 git-remote-http.exe，无法通过 https 拉取远程'
        return True, ''

    def download_git_full(self, git_root, on_line=None) -> bool:
        """下载完整版 git 替换 git_root 目录，失败自动回滚。返回是否成功。

        Args:
            git_root: 待替换的 git 安装根目录（如 ./toolkit/Git）
            on_line: 进度/日志回调
        """
        def emit(msg):
            if on_line:
                on_line(msg)
            else:
                logger.info(msg)

        tmp_dir = tempfile.mkdtemp(prefix='oas_git_upgrade_')
        archive_name = GIT_FULL_URLS[0].rsplit('/', 1)[-1]
        archive_path = os.path.join(tmp_dir, archive_name)
        extract_dir = os.path.join(tmp_dir, 'git_new')
        try:
            # 1. 下载（多源依次尝试），进度节流写入日志
            downloaded = False
            for url in GIT_FULL_URLS:
                emit(f'下载完整版 git {GIT_FULL_VERSION}（约 116MB，视网速可能需要几分钟）…')
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
                    self._download_archive(url, archive_path, on_progress=progress)
                    downloaded = True
                    break
                except Exception as e:
                    emit(f'下载失败({url}): {e}')
            if not downloaded:
                emit('所有下载源均失败')
                return False
            emit('下载完成')

            # 2. 解压校验（含 git-remote-http.exe，缺则替换了也拉不了远程）
            os.makedirs(extract_dir, exist_ok=True)
            emit('解压中（完整版体积较大，请耐心等待）…')
            self._extract_archive(archive_path, extract_dir)
            if not os.path.exists(os.path.join(extract_dir, 'mingw64', 'bin', 'git.exe')):
                emit('解压内容缺少 mingw64/bin/git.exe，结构异常')
                return False
            if not os.path.exists(os.path.join(
                    extract_dir, 'mingw64', 'libexec', 'git-core', 'git-remote-http.exe')):
                emit('解压内容缺少 git-remote-http.exe，无法通过 https 拉取，放弃替换')
                return False

            # 3. 备份 -> 替换 -> 验证，失败回滚
            backup = git_root + '.bak'
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            # 原目录可能已被删除，存在才备份
            if os.path.exists(git_root):
                shutil.move(git_root, backup)
            try:
                shutil.move(extract_dir, git_root)
            except Exception as e:
                self._restore_git(git_root, backup)
                emit(f'替换失败并已回滚: {e}')
                return False
            try:
                version_text = self.execute_output(
                    f'"{git_root}/mingw64/bin/git.exe" --version').strip()
                if not version_text:
                    raise Exception('git --version 无输出')
            except Exception as e:
                self._restore_git(git_root, backup)
                emit(f'新 git 验证失败并已回滚: {e}')
                return False
            shutil.rmtree(backup, ignore_errors=True)
            emit(f'git 已升级到 {version_text}')
            return True
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _download_archive(url, dest, on_progress=None) -> None:
        """流式下载归档到 dest，失败抛异常。"""
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

    @staticmethod
    def _extract_archive(path, dest) -> None:
        """解压下载的 git 归档到 dest，拒绝含路径穿越的成员。

        完整版 git 发行包是 .tar.bz2（Python 原生可解，无需外部 7z）；同时兼容 .zip。
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
        """替换失败时从备份恢复 git 目录。"""
        if os.path.exists(git_root):
            shutil.rmtree(git_root, ignore_errors=True)
        if os.path.exists(backup):
            shutil.move(backup, git_root)
