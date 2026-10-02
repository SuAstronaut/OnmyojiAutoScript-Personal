@rem OAS 独立更新器：在干净进程里完成 git 拉取 + pip 依赖 + OCR 依赖对齐。
@rem
@rem 为什么要单独一个入口而不是用 web 更新器：
@rem Windows 锁定已加载的 onnxruntime_providers_shared.dll，而 server.py / gui.py /
@rem script.py 入口都会 preload onnxruntime，Python 又无法卸载已加载的扩展 DLL。
@rem 所以 server 进程内发起的 OCR 换包必然 WinError 5 拒绝访问。
@rem 本脚本先停掉所有 OAS 进程释放 DLL，再在干净进程里对齐。
@echo off
chcp 65001 > nul

set "_root=%~dp0"
set "_root=%_root:~0,-1%"
cd /d "%_root%"

color F0
title OAS 更新器

set "_pyBin=%_root%\toolkit"
set "_GitBin=%_root%\toolkit\Git\mingw64\bin"
set "_adbBin=%_root%\toolkit\Lib\site-packages\adbutils\binaries"
set "PATH=%_root%\toolkit\alias;%_root%\toolkit\command;%_pyBin%;%_pyBin%\Scripts;%_GitBin%;%_adbBin%;%PATH%"
set "PYTHONIOENCODING=utf-8"

echo.
echo  OAS 更新器
echo  安装目录：%_root%
echo.
echo  即将停止所有 OAS 进程（GUI / server / 运行中的实例 / OCR 服务），
echo  这是换 onnxruntime 包的前提——DLL 被占用时无法替换。
echo.
pause

echo.
echo ===== 开始更新 =====
python -m deploy.update
if %errorlevel% neq 0 (
    echo.
    echo ===== 更新未完成 =====
    echo 上面已打印中断阶段与失败原因。若提示 onnxruntime 被占用，
    echo 请确认没有其它程序在用 toolkit\Lib\site-packages\onnxruntime 后重试。
    pause
    exit /b 1
)

echo.
echo ===== 更新完成，可以启动 OAS 了 =====
pause

