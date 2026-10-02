# This Python file uses the following encoding: utf-8
# 配置访问静态门禁：阻止生产代码绕过 ConfigStore 裸读/写 config/*.json。
#
# 三类旁路：
#   ① 写：Store/GenerationManager/utils 私有原语之外，对实例 config/*.json 的
#      写模式 open、write_file、Path rename/replace/unlink、write_text
#   ② 读：Store/GenerationManager 之外，对 config/*.json 的 glob/rglob、
#      read_file(filepath_config(...))、直接 read_text/open 读
#   ③ 已删除的 ConfigModel read/write/business writer（read_json/write_json/
#      script_set_arg/copy_script_task/copy_task_group/reset_datetime_for_all_enabled_tasks）
#
# 允许：config/tasks_config/ 运行期数据、module/config/argument/ 参数 schema、
#       部署配置（deploy.yaml）。
import argparse
import ast
import sys
from pathlib import Path

# 允许直接触碰 config 文件事务的模块（Store / generation / utils 私有原语）
ALLOWED_IOCORE_FILES = {
    "config_store.py",
    "config_generation.py",
    "utils.py",
}

# 仅允许在锁内使用的私有读写原语
ALLOWED_UNLOCKED_PRIMITIVES = {
    "_read_file_unlocked",
    "_write_file_unlocked",
    "read_file",
    "write_file",
}

# 已删除的 ConfigModel I/O / 业务 writer，禁止再被引用
REMOVED_CONFIG_MODEL_METHODS = {
    "read_json",
    "write_json",
    "script_set_arg",
    "copy_script_task",
    "copy_task_group",
    "reset_datetime_for_all_enabled_tasks",
}

# 不保留生产配置访问例外；所有实例配置读写都必须经过 ConfigStore。
ALLOWED_FUNCTION_EXCEPTIONS = {}

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "module", ROOT / "tasks", ROOT / "dev_tools"]
SCAN_FILES = [ROOT / "script.py", ROOT / "server.py"]


def _is_allowed_file(path: Path) -> bool:
    return path.name in ALLOWED_IOCORE_FILES


def _is_config_target(segment: str) -> bool:
    """启发式判断 AST 片段是否指向 config/*.json。

    filepath_config(...) 是显式标记；字符串里同时出现 'config' 与 '.json'
    （tasks_config/argument/deploy 除外）视为实例配置访问。
    """
    if "filepath_config" in segment:
        return True
    if "tasks_config" in segment or "/argument/" in segment or "deploy" in segment:
        return False
    return "config" in segment and ".json" in segment


def _call_source(node: ast.AST) -> str:
    return ast.get_source_segment("", node) if False else _render(node)


def _render(node: ast.AST) -> str:
    """把 AST 节点渲染成源码片段用于模式判断（不保证与原文逐字一致）。"""
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparse-error>"


def _open_is_write(args, keywords) -> bool:
    mode = None
    for kw in keywords:
        if kw.arg == "mode":
            mode = _render(kw.value)
            break
    if mode is None and len(args) >= 2:
        mode = _render(args[1])
    if mode is None:
        return False
    return any(ch in mode for ch in "wax+")


def scan_file(path: Path, violations: list[str]) -> None:
    rel = path.relative_to(ROOT).as_posix()
    if _is_allowed_file(path):
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        violations.append(f"{rel}: SYNTAX ERROR {e}")
        return

    def scan_scope(nodes, func_name):
        for node in nodes:
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    _check_call(rel, func_name, child, violations)

    # 模块顶层代码（不在任何函数内）
    top_level = [
        node for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    scan_scope(top_level, None)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan_scope([node], node.name)
        elif isinstance(node, ast.ClassDef):
            # 类内方法会被上面的 FunctionDef 分支覆盖；这里只处理类体内的顶层（属性等）
            pass


def _check_call(rel: str, func_name, node: ast.Call, violations: list[str]) -> None:
    func = node.func
    callee = _render(func)
    seg = _render(node)
    # ① 写 bypass：write_file / Path.write_text / Path.write_bytes
    if callee in ("write_file", "write_text", "write_bytes") and _is_config_target(seg):
        if not _in_exception(rel, func_name):
            violations.append(f"{rel}:{node.lineno}: write bypass {seg}")
    if callee in ("read_file", "read_text", "read_bytes") and _is_config_target(seg):
        if not _in_exception(rel, func_name):
            violations.append(f"{rel}:{node.lineno}: read bypass {seg}")
    if callee == "open":
        if _open_is_write(node.args, node.keywords) and _is_config_target(seg):
            if not _in_exception(rel, func_name):
                violations.append(f"{rel}:{node.lineno}: open(w) bypass {seg}")
        elif _is_config_target(seg):
            if not _in_exception(rel, func_name):
                violations.append(f"{rel}:{node.lineno}: open read bypass {seg}")
    # Path 方法旁路：glob/rglob/unlink/rename/replace 及 text/bytes 读写
    if isinstance(func, ast.Attribute) and func.attr in (
        "glob", "rglob", "unlink", "rename", "replace",
        "write_text", "write_bytes", "read_text", "read_bytes",
    ):
        if _is_config_target(seg):
            if not _in_exception(rel, func_name):
                violations.append(f"{rel}:{node.lineno}: path method {func.attr} {seg}")
    if isinstance(func, ast.Attribute) and func.attr in REMOVED_CONFIG_MODEL_METHODS:
        violations.append(f"{rel}:{node.lineno}: removed ConfigModel writer {seg}")


def _in_exception(rel: str, func_name) -> bool:
    return func_name is not None and (rel, func_name) in ALLOWED_FUNCTION_EXCEPTIONS


def main() -> int:
    parser = argparse.ArgumentParser(description="scan config access bypasses")
    parser.add_argument("--check", action="store_true", help="exit non-zero on violations")
    parser.add_argument("--limit", type=int, default=0, help="max violations before failing")
    args = parser.parse_args()

    violations: list[str] = []
    for directory in SCAN_DIRS:
        for path in sorted(directory.rglob("*.py")):
            scan_file(path, violations)
    for path in SCAN_FILES:
        if path.exists():
            scan_file(path, violations)

    for v in violations:
        print(v)
    print(f"scan_config_access: {len(violations)} violation(s)")
    if args.check and violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
