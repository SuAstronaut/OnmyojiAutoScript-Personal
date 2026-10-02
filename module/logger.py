# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import sys

import logging
import os
import shutil
from datetime import datetime, timedelta, date
from io import TextIOBase
from pathlib import Path
from rich.console import Console, ConsoleOptions, ConsoleRenderable, NewLine, RenderResult
from rich.highlighter import NullHighlighter
from rich.logging import RichHandler
from rich.rule import Rule
from typing import Callable, List


def cleanup_logs(log_dir: str = "./log", keep_days: int = 7):
    """删除 log_dir 下所有早于 keep_days 的文件夹和文件"""
    log_path = Path(log_dir)
    if not log_path.exists():
        return  # 目录都没有，直接退出
    keep_days_ago_ts = (datetime.now() - timedelta(days=keep_days)).timestamp()
    for name in os.listdir(log_path):
        full_path = os.path.join(log_path, name)
        # 忽略软链接，仅处理文件和目录
        if not os.path.exists(full_path):
            continue
        if os.path.isfile(full_path):
            # 处理 log 根目录下超过keep_days的文件
            try:
                if os.path.getmtime(full_path) < keep_days_ago_ts:
                    os.remove(full_path)
            except OSError as e:
                logger.error(f"delete file '{full_path}' error: {e}")
        elif os.path.isdir(full_path):
            # 检查是否为 error 目录
            if name != 'error':
                continue
            for error_dir_name in os.listdir(full_path):
                error_dir_path = os.path.join(full_path, error_dir_name)
                if not os.path.isdir(error_dir_path):
                    continue
                # 处理 log/error 根目录下超过keep_days的文件夹
                try:
                    if os.path.getmtime(error_dir_path) < keep_days_ago_ts:
                        # 递归删除整个目录及其内容
                        shutil.rmtree(error_dir_path)
                except OSError as e:
                    logger.error(f"delete dir '{error_dir_path}' error: {e}")


def empty_function(*args, **kwargs):
    pass


# Ensure running in Alas root folder
os.chdir(os.path.join(os.path.dirname(__file__), '../'))
# cnocr will set root logger in cnocr.utils
# Delete logging.basicConfig to avoid logging the same message twice.
logging.basicConfig = empty_function
logging.raiseExceptions = True  # Set True if wanna see encode errors on console

# Remove HTTP keywords (GET, POST etc.)
# RichHandler.KEYWORDS = []


# def show_handlers(handlers):
#     # 获取并打印日志记录器中处理器的信息
#     for handler in logger.handlers:
#         # 获取处理器的类名
#         handler_class = handler.__class__.__name__
#         print(f"Handler class: {handler_class}")
#
#         # 获取处理器的级别
#         handler_level = logging.getLevelName(handler.level)
#         print(f"Handler level: {handler_level}")
#
#         # 获取处理器的格式化器
#         formatter = handler.formatter
#         if formatter is not None:
#             formatter_class = formatter.__class__.__name__
#             print(f"Formatter class: {formatter_class}")
#
#         # 其他处理器的属性和方法，根据需要进行获取和打印
#         print()  # 打印空行，用于分隔处理器的信息

# GUI（Flutter 前端）日志渲染宽度，单位是显示列。
#
# 行首装饰占 23 列（级别 8 + `|` 1 + 时间戳 12 + `|` 1 + 空格 1）；
# 前端把毫秒三位截成一位后行首实际 21 列。
#
# 注意普通日志行**不再**按此宽度折行：FlutterHandler 用 soft_wrap 整行推出，
# 折行改由前端 normalizeLogLines 按显示列硬折（rich 不会断 CJK，见那里的注释）。
# 本常量现在只约束两处——traceback 的定宽框，以及 GuiRule 分隔线总宽。
#
# 取值沿革：最初 80（行首 24 列装饰，正文只余 56 列，实测 800 条真实日志有 8%
# 要折行）→ 试过 160（需把正文字号压到 12 才能在 1280 窗口装下整行）→ 100 +
# 行首压缩 → 回到 80 并保留行首压缩（23 列）→ 后端不再折行，宽度只管框与分隔线。
GUI_LOG_WIDTH = 80

# START/RESTART 分隔线总宽，与日志行同宽（80 列）。
# 标题仍由 GuiRule.__str__ 按总宽两侧均分保持居中。
# 前端的行宽硬上限 kLogLineWidthLimit（OASX lib/component/log/log_line_width.dart）
# 等于它——那边兜住不经 rich、完全没有宽度约束的子进程 stdout，超出分隔线的才折。
# 改这里必须同步改前端那个常量，否则子进程输出会溢出分隔线或被多折一次。
GUI_RULE_WIDTH = GUI_LOG_WIDTH

# Logger init
logger_debug = False
logger = logging.getLogger('oas')
logger.setLevel(logging.DEBUG if logger_debug else logging.INFO)
file_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d | %(filename)20s:%(lineno)04d | %(levelname)8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d │ %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
flutter_formatter = logging.Formatter(
    # 行首压缩到 23 列：级别 8 + `|` + 时间戳 12 + `|` + 空格。
    # 竖线两侧不留空格（原来是 ` | `），等宽字体下靠列位本身就成列，省 4 列给正文。
    #
    # 级别由本 formatter 输出而非 rich 的级别列（set_func_logger 里
    # show_level=False）：rich 的列会在 ljust(8) 后再加一个列间空格，凑成 9 列，
    # 且把折行的续行缩进对齐到正文列。改由 formatter 出级别有两个好处——
    # 级别恰好 8 列（CRITICAL 也是 8，不会像 ljust(7) 那样多占一列破坏对齐），
    # 续行从第 0 列起排、每条续行多得 9 列正文。代价是续行不再有悬挂缩进。
    fmt='%(levelname)-8s|%(asctime)s.%(msecs)03d| %(message)s', datefmt='%H:%M:%S')


# ======================================================================================================================
#            Set console logger
# ======================================================================================================================
console_hdlr = RichHandler(
    console=Console(
        width=120,
        # force_terminal=False：明确告知 rich 目标不是真实终端，
        # 避免在 mshta 隐藏窗口/后台服务等场景下走 Win32 legacy 控制台渲染，
        # 否则 WriteConsole 会因控制台句柄处于退化状态抛 [Errno 22] Invalid argument
        force_terminal=False
    ),
    show_path=False,
    show_time=False,
    rich_tracebacks=True,
    tracebacks_show_locals=True,
    tracebacks_extra_lines=3,
    tracebacks_width=160
)
console_hdlr.setFormatter(console_formatter)
logger.addHandler(console_hdlr)


# ======================================================================================================================
#            Set file
# ======================================================================================================================
def _open_log_file(name: str, day: date):
    """打开指定日期的日志文件，log 目录缺失时补建。

    Args:
        name: 日志名（通常是实例名，如 oas1）。
        day: 日志归属日期，决定文件名前缀。

    Returns:
        (文件对象, 文件路径) 二元组。
    """
    log_file = f'./log/{day}_{name}.txt'
    try:
        file = open(log_file, mode='a', encoding='utf-8')
    except FileNotFoundError:
        os.makedirs('./log', exist_ok=True)
        file = open(log_file, mode='a', encoding='utf-8')
    return file, log_file


def _close_handler_file(handler) -> None:
    """关闭被替换掉的文件 handler 及其持有的文件对象，避免文件描述符泄漏。"""
    file = getattr(getattr(handler, 'console', None), 'file', None)
    try:
        handler.close()
    except Exception:
        pass
    if file is not None and not getattr(file, 'closed', True):
        try:
            file.close()
        except OSError:
            pass


class RichFileHandler(RichHandler):
    """按日期滚动的文件日志 handler。

    进程若在零点前启动并持续运行，启动时算出的文件名会把次日日志继续追加进前一天的
    文件；而读取侧（module/server/log_stats.py、log_service.py）是按 date.today()
    找文件的，于是当天统计读不到数据，前一天的统计里又混进了今天的日志。
    这里在每次写入前比对日期，跨天就关掉旧文件换到新日期的文件。
    """

    def __init__(self, *args, log_name: str, log_day: date, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_name = log_name
        self.log_day = log_day
        # 防重入：滚动过程中补写分隔块与提示行会再次进入写入路径
        self._rolling = False

    def emit(self, record: logging.LogRecord) -> None:
        """写入前先判断是否需要跨天切换文件，并让 [STAT] 行整行落盘。"""
        self.maybe_rollover()
        # [STAT] 是机器协议，必须整行落盘。Rich 的定宽折行可能丢掉断点空格并
        # 补入行尾空格，使读者无法恢复原始 JSON；跳过 Table 和宽度限制才能保真。
        # 普通日志仍按 160 列渲染，逐条复位避免影响下一条记录。
        self.console.soft_next = self._is_stat(record)
        try:
            super().emit(record)
        finally:
            self.console.soft_next = False

    def render(self, *, record, traceback, message_renderable):
        """[STAT] 行直接返回消息本身，绕开 LogRender 的定宽 Table。

        Table 按列宽折行、不受 Console 的 soft_wrap 影响，所以要真正不折，
        这里与 SoftWrapConsole 两处都得改（同 FlutterHandler.render 的说明）。
        """
        if traceback is None and self._is_stat(record):
            return message_renderable
        return super().render(record=record, traceback=traceback,
                              message_renderable=message_renderable)

    @staticmethod
    def _is_stat(record: logging.LogRecord) -> bool:
        """本条记录是否为 [STAT] 协议行。"""
        try:
            return str(record.getMessage()).startswith('[STAT] ')
        except Exception:
            # getMessage 会对消息做 % 插值，坏格式不该让日志本身写不出去
            return False

    def maybe_rollover(self) -> None:
        """日期与当前文件归属日不一致时切换文件；同一天为空操作。"""
        if self._rolling or date.today() == self.log_day:
            return
        # Handler.lock 是 RLock，emit() 经 handle() 已持锁时可重入
        with self.lock:
            # 双检：并发写入时另一线程可能已完成切换
            if self._rolling or date.today() == self.log_day:
                return
            self._rolling = True
            try:
                self._rollover()
            finally:
                self._rolling = False

    def _rollover(self) -> None:
        """关闭旧日期文件，切到新日期文件，并在新文件开头补写 START 分隔块。"""
        previous_day = self.log_day
        old_file = self.console.file

        new_day = date.today()
        file, log_file = _open_log_file(self.log_name, new_day)
        self.console.file = file
        self.log_day = new_day
        logger.log_file = log_file

        if old_file is not None and not getattr(old_file, 'closed', True):
            try:
                old_file.close()
            except OSError:
                pass

        # 新文件补写 START 分隔块：统计解析（log_stats.py）以 ═/─标题─/═ 三行块作为
        # 任务与会话边界，script.py 的 save_error_log 也靠最后一个 ═ 行定位切片起点，
        # 缺了这个块新文件会退化成"无边界日志"。只写文件，不广播到控制台/GUI。
        self.console.print(GuiRule(title='', characters='═'))
        self.console.print(GuiRule(title='START', characters='─'))
        self.console.print(GuiRule(title='', characters='═'))
        logger.info(f'Log file rolled over: {previous_day} -> {new_day}')


# Add file logger
pyw_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
if pyw_name in ('', '-', '-c'):
    pyw_name = 'script'


def set_file_logger(name=pyw_name, *, do_cleanup=False):
    log_day = date.today()
    file, log_file = _open_log_file(name, log_day)

    # 用 SoftWrapConsole 而非普通 Console：RichFileHandler 要对 [STAT] 协议行
    # 逐条开 soft_wrap 整行推出（见那里的说明）。普通日志行不带 soft_next，
    # 行为与改动前完全一致，仍按 160 列折行。
    file_console = SoftWrapConsole(
        file=file,
        no_color=True,
        highlight=False,
        width=160,
    )

    hdlr = RichFileHandler(
        console=file_console,
        show_path=False,
        show_time=False,
        show_level=False,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
        tracebacks_extra_lines=3,
        tracebacks_width=160,
        highlighter=NullHighlighter(),
        log_name=name,
        log_day=log_day,
    )
    hdlr.setFormatter(file_formatter)

    stale_handlers = [h for h in logger.handlers if isinstance(
        h, (logging.FileHandler, RichFileHandler))]
    logger.handlers = [h for h in logger.handlers if h not in stale_handlers]
    # 被替换的旧 handler 要显式关闭，否则重复调用会累积未关闭的日志文件句柄
    for stale in stale_handlers:
        _close_handler_file(stale)
    logger.addHandler(hdlr)
    logger.log_file = log_file

    # ---------- 可选：清理旧文件 ----------
    if do_cleanup:
        cleanup_logs()
        logger.info("Log cleanup finished")


# ======================================================================================================================
#            Set flutter
# ======================================================================================================================
class FlutterHandler(RichHandler):
    """推给前端的 handler：普通日志行整行推出，不在后端折行。

    为什么不让 rich 折：rich 的折行按「单词」切（rich/_wrap.py 的
    `re_word = r"\\s*\\S+\\s*"`），而中文整段没有空格，一条 34 字的中文消息
    就是一个 68 列的「单词」。行首装饰占 23 列后只剩 57 列放不下它，
    `divide_line` 于是在词首插换行——第一行只剩 `WARNING |07:42:27.350|`，
    正文整段掉到第二行且不带行首。用户要求的
    `WARNING |07:42:27.3| 内容` 格式就是这么被破坏的。

    rich 没有 CJK 断行规则，调宽度或 no_wrap 都救不了（no_wrap 会直接截断丢内容）。
    而前端 `normalizeLogLines`（OASX lib/component/log/log_line_width.dart）本来就
    按显示列逐字符硬折、CJK 计 2 列、不切开代理对，能力强于 rich。因此分工改成
    「后端只保证格式，前端负责折行」。

    要真正不折，两处都得改，缺一不可（实测只做前者仍折成两行）：
      1. `render()` 不再把消息塞进 `LogRender` 的 Table —— Table 按列宽折行，
         不受 Console 的 soft_wrap 影响；
      2. Console 那次 print 传 soft_wrap=True，见 SoftWrapConsole。

    soft_wrap 同时关掉了 ljust 补位，载荷体积约减半（补位空格前端本来就要剥掉）。
    traceback 例外：它是 rich 画的定宽框（┌─┐ 边框按 console 宽度对齐），
    折行会毁掉框线，所以带 exc_info 的记录仍走 rich 原本的 Table + 定宽渲染。
    """

    def emit(self, record: logging.LogRecord) -> None:
        # 只有普通日志行走 soft_wrap；traceback 必须保留 rich 的定宽框渲染。
        # 用 setattr 而非要求 console 是 SoftWrapConsole：万一将来换成普通
        # Console，这里只是多设一个无人读的属性，不会 AttributeError 崩掉日志。
        self.console.soft_next = not self._has_traceback(record)
        try:
            super().emit(record)
        finally:
            self.console.soft_next = False

    def render(self, *, record, traceback, message_renderable):
        """无 traceback 时直接返回消息本身，绕开 LogRender 的定宽 Table。"""
        if traceback is None:
            return message_renderable
        return super().render(record=record, traceback=traceback,
                              message_renderable=message_renderable)

    def _has_traceback(self, record: logging.LogRecord) -> bool:
        """本条记录是否会被 rich 渲染成 traceback 框（判定与 RichHandler.emit 一致）。"""
        return bool(self.rich_tracebacks and record.exc_info
                    and record.exc_info != (None, None, None))


class FlutterConsole(Console):
    """
    Force full feature console
    but not working lol :(
    """

    @property
    def options(self) -> ConsoleOptions:
        return ConsoleOptions(
            max_height=self.size.height,
            size=self.size,
            legacy_windows=False,
            min_width=1,
            max_width=self.width,
            encoding='utf-8',
            is_terminal=False,
        )


class FlutterLogStream(TextIOBase):
    def __init__(self, *args, func: Callable[[ConsoleRenderable], None] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._func = func

    def write(self, msg: str) -> int:
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8")
        self._func(msg)
        return len(msg)


class SoftWrapConsole(Console):
    """按 soft_next 逐条切换是否走 soft_wrap 的 Console。

    soft_wrap 只能在 print() 调用时传，而 RichHandler.emit 内部那次 print 不接受
    外部参数，所以由使用它的 handler 在 emit 中先置 soft_next，这里的 print 再取用。
    handler 持锁逐条 emit，并在 finally 里复位，不会跨记录串味。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.soft_next = False

    def print(self, *objects, **kwargs):
        if self.soft_next:
            kwargs.setdefault('soft_wrap', True)
        return super().print(*objects, **kwargs)


def set_func_logger(func):
    stream = FlutterLogStream(func=func)
    stream_console = SoftWrapConsole(
        file=stream,
        force_terminal=False,
        force_interactive=False,
        no_color=True,
        highlight=False,
        # 普通日志行走 soft_wrap 不受此宽度约束（见 FlutterHandler），
        # 这里的宽度只对 traceback 的定宽框生效。
        width=GUI_LOG_WIDTH,
    )
    hdlr = FlutterHandler(
        console=stream_console,
        show_path=False,
        show_time=False,
        # 级别改由 flutter_formatter 输出（见那里的注释）：rich 的级别列会额外占
        # 一个列间空格并给续行加悬挂缩进，关掉后行首恰好 8 列、续行满宽。
        show_level=False,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
        tracebacks_extra_lines=3,
        highlighter=NullHighlighter(),
    )
    hdlr.setFormatter(flutter_formatter)
    logger.addHandler(hdlr)

# ======================================================================================================================
#            Set print format
# ======================================================================================================================


def _get_renderables(
        self: Console, *objects, sep=" ", end="\n", justify=None, emoji=None, markup=None, highlight=None,
) -> List[ConsoleRenderable]:
    """
    Refer to rich.console.Console.print()
    """
    if not objects:
        objects = (NewLine(),)

    render_hooks = self._render_hooks[:]
    with self:
        renderables = self._collect_renderables(
            objects,
            sep,
            end,
            justify=justify,
            emoji=emoji,
            markup=markup,
            highlight=highlight,
        )
        for hook in render_hooks:
            renderables = hook.process_renderables(renderables)
    return renderables


def print(*objects: ConsoleRenderable, **kwargs):
    for hdlr in logger.handlers:
        try:
            # rule()/hr() 经此直接写 console，绕过 emit()，跨天时同样需要先切文件，
            # 否则三行边界块会被拆到两个文件里，统计解析将识别不到该边界
            if isinstance(hdlr, RichFileHandler):
                hdlr.maybe_rollover()
            if isinstance(hdlr, FlutterHandler):
                for renderable in _get_renderables(hdlr.console, *objects, **kwargs):
                    hdlr.console.file._func(str(renderable))
            elif isinstance(hdlr, RichHandler):
                hdlr.console.print(*objects)
        except OSError:
            # 控制台句柄失效时不能让日志输出中断业务流程，文件日志会继续由其他 handler 记录。
            continue


class GuiRule(Rule):
    """分隔线：固定 [GUI_RULE_WIDTH] 列，与日志行同宽（80 列）。

    标题靠 __str__ 里按总宽两侧均分保持居中。

    注意本类同时服务于文件日志（RichFileHandler.maybe_rollover 的 START 块），
    那侧 Console 是 width=160，比本常量宽，分隔线画完右侧留白——历史行为，不变。
    统计解析（module/server/log_stats.py）的 ═{15,} / ─{10,} 正则不限定具体宽度，
    改宽不影响任务与会话边界识别。
    """

    def __rich_console__(
            self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        options.max_width = GUI_RULE_WIDTH
        return super().__rich_console__(console, options)

    def __str__(self):
        total_width = GUI_RULE_WIDTH
        cell_len = len(self.title) + 2
        aside_len = (total_width - cell_len) // 2
        left = self.characters * aside_len
        right = self.characters * (total_width - cell_len - aside_len)
        if self.title:
            space = ' '
        else:
            space = self.characters
        return f"{left}{space}{self.title}{space}{right}\n"

    def __repr__(self):
        return self.__str__()


def rule(title="", *, characters="─", style="rule.line", end="\n", align="center"):
    rule = GuiRule(title=title, characters=characters,
                   style=style, end=end)
    print(rule)


def hr(title, level=3):
    title = str(title).upper()
    if level == 1:
        logger.rule(title, characters='═')
        logger.info(title)
    if level == 2:
        logger.rule(title, characters='─')
        logger.info(title)
    if level == 3:
        logger.info(f"[bold]<<< {title} >>>[/bold]", extra={"markup": True})
    if level == 0:
        logger.rule(characters='═')
        logger.rule(title, characters='─')
        logger.rule(characters='═')


def attr(name, text):
    logger.info('[%s] %s' % (str(name), str(text)))


def attr_align(name, text, front='', align=22):
    name = str(name).rjust(align)
    if front:
        name = front + name[len(front):]
    logger.info('%s: %s' % (name, str(text)))


def show():
    logger.info('INFO')
    logger.warning('WARNING')
    logger.debug('DEBUG')
    logger.error('ERROR')
    logger.critical('CRITICAL')
    logger.hr('hr0', 0)
    logger.hr('hr1', 1)
    logger.hr('hr2', 2)
    logger.hr('hr3', 3)
    logger.info(r'Brace { [ ( ) ] }')
    logger.info(r'True, False, None')
    logger.info(r'E:/path\\to/alas/alas.exe, /root/alas/, ./relative/path/log.txt')
    logger.info('Tests very long strings. Tests very long strings. Tests very long strings. Tests very long strings. Tests very long strings.')
    local_var1 = 'This is local variable'
    # Line before exception
    raise Exception("Exception")
    # Line below exception


def error_convert(func):
    def error_wrapper(msg, *args, **kwargs):
        if isinstance(msg, Exception):
            msg = f'{type(msg).__name__}: {msg}'
        return func(msg, *args, **kwargs)

    return error_wrapper


logger.error = error_convert(logger.error)
logger.hr = hr
logger.attr = attr
logger.attr_align = attr_align
logger.set_file_logger = set_file_logger
logger.set_func_logger = set_func_logger
logger.rule = rule
logger.print = print
logger.log_file: str

logger.set_file_logger()
logger.hr('Start', level=0)
