"""独立寄售券离线测试：执行真实源码，隔离游戏设备与应用日志初始化。"""
import ast
import copy
import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from filelock import FileLock
from pydantic import BaseModel, Field

from tasks.MultiDailyAltAcc.task_plan import (
    DEFAULT_TASK_PLAN, TaskPlanError, load_task_plan, parse_task_plan,
)

ROOT = Path(__file__).resolve().parents[2]


def source(path, namespace, names=None):
    """只加载所选定义，保留真实方法体，避免 Config/Device/logger 导入副作用。"""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.Assign, ast.AnnAssign))
             and (names is None or getattr(node, 'name', None) in names)]
    for node in nodes:
        if isinstance(node, ast.ClassDef) and node.name not in ('ProgressStore',):
            node.bases = [ast.Name(id='object', ctx=ast.Load())]
        if isinstance(node, ast.ClassDef) and node.name.endswith('Config'):
            node.bases = [ast.Name(id='BaseModel', ctx=ast.Load())]
        if isinstance(node, ast.ClassDef) and node.name == 'ExtendedAccountInfo':
            node.bases = [ast.Name(id='BaseModel', ctx=ast.Load())]
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[]))
    exec(compile(module, str(ROOT / path), 'exec'), namespace)
    return namespace


class TaskEnd(Exception):
    pass


class GameStuckError(Exception):
    pass


NS = dict(logger=Mock(), json=json, os=os, datetime=datetime, timedelta=timedelta,
          Path=Path, FileLock=FileLock, threading=threading,
          time=SimpleNamespace(time=time.time, sleep=lambda _: None),
          BaseModel=BaseModel, Field=Field, TaskEnd=TaskEnd, GameStuckError=GameStuckError,
          page_main='main', page_mall='mall', GuildStore=SimpleNamespace,
          Consignment=SimpleNamespace,
          StatEvent=SimpleNamespace(TASK_START='task_start', TASK_END='task_end', ERROR='error',
                                    ACC_START='acc_start'),
          GameUiAssets=SimpleNamespace(), WantedQuestsAssets=SimpleNamespace(
              I_WQ_SEAL=object(), I_WQ_DONE=object()))
for error in ('EmulatorNotRunningError', 'GameBugError', 'GameNotRunningError',
              'GamePageUnknownError', 'GameTooManyClickError', 'RequestHumanTakeover', 'ScriptError'):
    NS[error] = type(error, (Exception,), {})
source('tasks/Component/MultiAccountRunner/progress.py', NS)
BaseProgress = NS['ProgressStore']
NS['_BaseProgressStore'] = BaseProgress
source('tasks/MultiDailyAltAcc/progress.py', NS)
ProgressStore = NS['ProgressStore']
source('tasks/DailyAltAcc/config.py', NS, {'DailyAltAccConfig'})
DailyConfig = NS['DailyAltAccConfig']
source('tasks/MultiDailyAltAcc/config.py', NS, {'MultiDailyAltAccConfig', 'ExtendedAccountInfo'})
MultiConfig = NS['MultiDailyAltAccConfig']
NS['DailyAltAcc'] = lambda: SimpleNamespace(daily_alt_acc_config=DailyConfig())
source('tasks/MultiDailyAltAcc/DailyAltAccEx.py', NS, {'get_config'})
injected_config = NS['get_config']
source('tasks/DailyAltAcc/script_task.py', NS, {'ScriptTask'})
DailyTask = NS['ScriptTask']
source('tasks/DailyAltAcc/mshop.py', NS, {'Mshop'})
Mshop = NS['Mshop']
source('tasks/RichMan/mall/consignment.py', NS, {'Consignment'})
ConsignmentTask = NS['Consignment']
# mshop.py 使用配置模型，不是同名的购买执行类。
NS['Consignment'] = SimpleNamespace
source('tasks/MultiDailyAltAcc/script_task.py', NS, {'ScriptTask'})
MultiTask = NS['ScriptTask']


def make_multi(plan=None, **flags):
    task = MultiTask()
    task.daily_conf = SimpleNamespace(multi_daily_alt_acc_config=MultiConfig(**flags))
    task.config = SimpleNamespace(model=SimpleNamespace(multi_daily_alt_acc=task.daily_conf))
    task._task_plan = plan or parse_task_plan(copy.deepcopy(DEFAULT_TASK_PLAN))
    task.set_next_run = Mock()
    task.save_config = Mock()
    return task


def make_daily(week=False, ticket=False, store=None):
    task = DailyTask()
    config = DailyConfig(weekaward_enable=week, consignment_ticket_enable=ticket,
                         donatejade_enable=False, courtyard_enable=False, mail_enable=False,
                         cooperation_enable=False, tree_planting_enable=0,
                         trialbattle_enable=False, summon_up_enable=False)
    task.get_config = lambda: SimpleNamespace(daily_alt_acc_config=config)
    task.device = SimpleNamespace(click_record_clear=lambda: None, image=None)
    task.screenshot = Mock()
    task.set_next_run = Mock()
    task.emit_stat = Mock()
    task.guild_popup_scope = nullcontext
    task.calls = []
    task._progress, task._progress_key = store, 'account'
    task.state, task.remaining = 'main', 10
    task.I_NET_NORMAL_FLAG, task.I_UI_BACK_RED = object(), object()
    task.I_NET_CHECK, task.I_BACK_YELLOW = object(), object()
    task.I_CON_ENTER, task.I_CON_ENTER_CHECK, task.I_CON_TICKET = object(), object(), object()
    task.C_NET_CLICK = object()
    network = iter((True, False))
    task.appear = lambda rule, **kwargs: (
        next(network) if rule is task.I_NET_NORMAL_FLAG else rule is task.I_UI_BACK_RED)
    task.ui_click_until_disappear = Mock()
    task.ui_get_current_page = lambda: task.state

    def goto(page, **kwargs):
        task.state = page
        task.calls.append(('goto', page))

    def back(rule, **kwargs):
        if rule is task.I_BACK_YELLOW:
            task.state = 'main'
            return True
        return False

    def mall_root():
        task.state = 'mall'
        task.calls.append(('back_root',))
        return True

    def buy(rule, number=None):
        task.calls.append(('buy', rule, number))
        task.remaining = 0

    task.ui_goto = goto
    task.appear_then_click = back
    task._back_to_mall_root = mall_root
    task._enter_consignment = lambda: setattr(task, 'state', 'consignment')
    task.ui_click = Mock()
    task.mall_check_money_legacy = lambda *args: True
    task.O_CON_NUMBER = SimpleNamespace(ocr=lambda image: task.remaining)
    task.buy_more = buy
    task.buy_one = Mock(side_effect=AssertionError('整数不得传给 buy_one'))
    task.execute_consignment = lambda config: ConsignmentTask.execute_consignment(task, config)
    task._buy_medal_black_daruma = lambda: task.calls.append(('medal',))
    task.execute_guild = lambda config: task.calls.append(('guild',))
    task.execute_mall = lambda: Mshop.execute_mall(task)
    task._share_collect = lambda: task.calls.append(('share',))
    task.run_mysteryshop = Mock(side_effect=AssertionError('不应调用神秘商店'))
    task.run_consignment_ticket = lambda: Mshop.run_consignment_ticket(task)
    return task


class ConsignmentTicketTests(unittest.TestCase):
    def run_daily(self, task):
        with self.assertRaises(TaskEnd):
            task.run()

    def test_ticket_only(self):
        task = make_daily(ticket=True)
        task.execute_mall = Mock(side_effect=AssertionError('不能调用整个商城'))
        task.execute_guild = Mock(side_effect=AssertionError('不能购买蓝票'))
        task.execute_medal = Mock(side_effect=AssertionError('不能访问勋章屋'))
        task._buy_medal_black_daruma = Mock(side_effect=AssertionError('不能买黑蛋'))
        task._share_collect = Mock(side_effect=AssertionError('不能分享'))
        self.run_daily(task)
        self.assertEqual(sum(c[0] == 'buy' for c in task.calls), 1)
        self.assertEqual(task.state, 'main')
        task.run_mysteryshop.assert_not_called()

    def test_weekaward_only_unchanged(self):
        task = make_daily(week=True)
        task.run_consignment_ticket = Mock(side_effect=AssertionError('独立开关关闭'))
        self.run_daily(task)
        self.assertEqual([c[0] for c in task.calls if c[0] in ('guild', 'medal', 'buy', 'share')],
                         ['guild', 'medal', 'buy', 'share'])

    def test_both_disabled(self):
        task = make_daily()
        self.run_daily(task)
        self.assertFalse(task.calls)

    def test_both_enabled_sold_out_second_entry(self):
        task = make_daily(week=True, ticket=True)
        entered = []
        task._enter_consignment = lambda: entered.append(task.remaining)
        self.run_daily(task)
        self.assertEqual(entered, [10, 0])
        self.assertEqual(sum(c[0] == 'buy' for c in task.calls), 1)
        self.assertEqual(task.state, 'main')

    def test_partial_quantity_uses_existing_buy_api(self):
        for remaining in (0, 10, 1, 5, 9):
            with self.subTest(remaining=remaining):
                task = make_daily(ticket=True)
                task.remaining = remaining
                task.buy_more = Mock()
                task.run_consignment_ticket()
                if remaining == 0:
                    task.buy_more.assert_not_called()
                elif remaining == 10:
                    task.buy_more.assert_called_once_with(task.I_CON_TICKET)
                else:
                    task.buy_more.assert_called_once_with(task.I_CON_TICKET, number=remaining)
                task.buy_one.assert_not_called()
                self.assertEqual(task.state, 'main')

    def test_buy_api_signatures(self):
        api = source('tasks/Component/Buy/buy.py', dict(NS), {'Buy'})['Buy']
        self.assertEqual(list(inspect.signature(api.buy_one).parameters),
                         ['self', 'start_click', 'check_image'])
        self.assertEqual(list(inspect.signature(api.buy_more).parameters),
                         ['self', 'start_click', 'number'])

    def test_exit_failure_is_device_error(self):
        task = make_daily(ticket=True)
        task._back_to_mall_root = lambda: False
        with self.assertRaises(GameStuckError):
            task.run_consignment_ticket()
        self.assertNotEqual(task.state, 'main')

    def test_insufficient_money_returns_to_main(self):
        task = make_daily(ticket=True)
        task.mall_check_money_legacy = lambda *args: False
        task.run_consignment_ticket()
        self.assertFalse(any(c[0] == 'buy' for c in task.calls))
        self.assertEqual(task.state, 'main')

    def test_config_defaults_do_not_inherit_weekaward(self):
        self.assertFalse(DailyConfig(weekaward_enable=True).consignment_ticket_enable)
        self.assertFalse(MultiConfig(total_weekaward_enable=True).total_consignment_ticket_enable)
        self.assertNotIn('consignment_ticket_enable', NS['ExtendedAccountInfo'].model_fields)

    def test_ui_defaults_and_labels(self):
        template = json.loads((ROOT / 'config/template.json').read_text(encoding='utf-8'))
        labels = json.loads((ROOT / 'assets/i18n/zh-CN.json').read_text(encoding='utf-8'))
        self.assertFalse(template['daily_alt_acc']['daily_alt_acc_config']['consignment_ticket_enable'])
        self.assertFalse(template['multi_daily_alt_acc']['multi_daily_alt_acc_config']['total_consignment_ticket_enable'])
        for key in ('consignment_ticket_enable', 'total_consignment_ticket_enable'):
            self.assertEqual(labels[key], '购买寄售券')

    def test_daily_injection_independent_of_account_weekaward(self):
        multi = make_multi(total_consignment_ticket_enable=True, total_weekaward_enable=False)
        account = multi._create_account_config(NS['ExtendedAccountInfo'](weekaward_enable=False))
        instance = SimpleNamespace(account_info=account, daily_conf=multi.daily_conf,
                                   config=SimpleNamespace(daily_alt_acc=SimpleNamespace(
                                       daily_alt_acc_config=DailyConfig())))
        daily = injected_config(instance).daily_alt_acc_config
        self.assertTrue(daily.consignment_ticket_enable)
        self.assertFalse(daily.weekaward_enable)
        self.assertEqual(multi._enabled_task_keys(account, True).count('consignment_ticket'), 1)

    def test_old_plan_missing_ticket_defaults_off_without_mutation(self):
        raw = copy.deepcopy(DEFAULT_TASK_PLAN)
        for phase in ('morning', 'afternoon'):
            del raw[phase]['consignment_ticket']
            raw[phase]['weekaward'] = True
        del raw['schedule']['consignment_ticket_weekdays']
        raw['schedule']['weekaward_weekdays'] = [3]
        before = copy.deepcopy(raw)
        plan = parse_task_plan(raw)
        self.assertFalse(plan.enabled('morning', 'consignment_ticket'))
        self.assertFalse(plan.enabled('afternoon', 'consignment_ticket'))
        self.assertEqual(plan.consignment_ticket_weekdays, (0,))
        self.assertEqual(raw, before)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(raw), encoding='utf-8')
            before_bytes = path.read_bytes()
            load_task_plan(path)
            self.assertEqual(path.read_bytes(), before_bytes)

    def test_plan_validation_and_removed_key_compatibility(self):
        raw = copy.deepcopy(DEFAULT_TASK_PLAN)
        raw['morning']['complete_cooperation'] = True
        self.assertTrue(parse_task_plan(raw).enabled('morning', 'consignment_ticket'))
        for value in ([True], [7], [-1], 'Monday'):
            with self.subTest(value=value):
                bad = copy.deepcopy(raw)
                bad['schedule']['consignment_ticket_weekdays'] = value
                with self.assertRaises(TaskPlanError):
                    parse_task_plan(bad)
        for field, value in (('consignment_ticket', 1), ('unknown', True)):
            bad = copy.deepcopy(raw)
            bad['morning'][field] = value
            with self.assertRaises(TaskPlanError):
                parse_task_plan(bad)
        del raw['morning']['mail']
        with self.assertRaises(TaskPlanError):
            parse_task_plan(raw)

    def test_weekday_materialization(self):
        raw = copy.deepcopy(DEFAULT_TASK_PLAN)
        raw['morning']['weekaward'] = False
        raw['morning']['mysteryshop'] = False
        for weekday in range(7):
            with self.subTest(weekday=weekday):
                task = make_multi(parse_task_plan(raw))
                task._schedule_plan_phase('morning', datetime(2026, 10, 5 + weekday, 1))
                base = task.daily_conf.multi_daily_alt_acc_config
                self.assertEqual(base.total_consignment_ticket_enable, weekday == 0)
                self.assertFalse(base.total_weekaward_enable)
                self.assertFalse(base.total_mysteryshop_enable)

    def test_custom_and_empty_weekdays(self):
        for days in ([2], []):
            raw = copy.deepcopy(DEFAULT_TASK_PLAN)
            raw['schedule']['consignment_ticket_weekdays'] = days
            task = make_multi(parse_task_plan(raw))
            task._schedule_plan_phase('morning', datetime(2026, 10, 7, 1))
            self.assertEqual(task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable,
                             bool(days))

    def test_afternoon_independent_selection(self):
        raw = copy.deepcopy(DEFAULT_TASK_PLAN)
        raw['afternoon']['consignment_ticket'] = True
        task = make_multi(parse_task_plan(raw))
        task._schedule_normal_day(datetime(2026, 10, 5, 12))
        self.assertTrue(task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable)
        self.assertFalse(task.daily_conf.multi_daily_alt_acc_config.total_weekaward_enable)

    def test_special_rounds_filter_ticket(self):
        for flag in ('total_returngift_enable', 'total_alliedteam_battle_enable'):
            with self.subTest(flag=flag):
                task = make_multi(total_consignment_ticket_enable=True, **{flag: True})
                task._apply_single_purpose_filter(task.daily_conf.multi_daily_alt_acc_config)
                self.assertFalse(task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable)

    def test_special_round_scheduling_clears_ticket(self):
        for method, args in (('_schedule_evening', (datetime(2026, 10, 5, 18),)),
                             ('_schedule_alliedteam_after_returngift', ())):
            with self.subTest(method=method):
                task = make_multi(total_consignment_ticket_enable=True)
                getattr(task, method)(*args)
                self.assertFalse(task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable)

    def test_rotation_disabled_keeps_manual_ticket(self):
        task = make_multi(disable_task_rotation=True, total_consignment_ticket_enable=True)
        task._apply_single_purpose_filter(task.daily_conf.multi_daily_alt_acc_config)
        task._schedule_by_scheduler()
        self.assertTrue(task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable)

    def test_snapshot_has_independent_flag(self):
        task = make_multi(total_weekaward_enable=False, total_consignment_ticket_enable=True)
        flags = NS['phase_flags_of'](task.daily_conf.multi_daily_alt_acc_config, 'morning')
        self.assertTrue(flags['total_consignment_ticket_enable'])
        self.assertFalse(flags['total_weekaward_enable'])

    def test_old_progress_does_not_copy_weekaward_status(self):
        for status in ('done', 'failed', 'skipped'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store = ProgressStore('test', directory)
                data = dict(phase_flags={'total_weekaward_enable': True}, accounts={
                    'account': dict(status='done', tasks={'weekaward': {'status': status}})})
                store.path.write_text(json.dumps(data), encoding='utf-8')
                loaded = store._load()
                self.assertNotIn('consignment_ticket', loaded['accounts']['account']['tasks'])
                self.assertNotIn('total_consignment_ticket_enable', loaded['phase_flags'])

    def test_old_account_done_reopens_only_for_pending_ticket(self):
        task = make_multi(total_consignment_ticket_enable=True)
        task._progress_key_of = lambda account: 'account'
        for status in (None, 'pending', 'done', 'failed', 'skipped'):
            with self.subTest(status=status):
                task._progress = SimpleNamespace(
                    is_account_done=lambda key: True,
                    is_task_finished=lambda key, name: status in ('done', 'failed', 'skipped'))
                self.assertEqual(task._should_process_account(SimpleNamespace(character='test')),
                                 status in (None, 'pending'))
        task.daily_conf.multi_daily_alt_acc_config.total_consignment_ticket_enable = False
        task._progress.is_task_finished = lambda *args: False
        self.assertFalse(task._should_process_account(SimpleNamespace(character='test')))

    def test_resume_skips_weekaward_but_runs_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProgressStore('test', directory)
            store._data = {'accounts': {'account': dict(status='done', tasks={
                'weekaward': {'status': 'done'}})}}
            task = make_daily(week=True, ticket=True, store=store)
            self.run_daily(task)
            self.assertFalse(any(c[0] in ('guild', 'medal', 'share') for c in task.calls))
            self.assertEqual(store.get_task('account', 'weekaward')['status'], 'done')
            self.assertEqual(store.get_task('account', 'consignment_ticket')['status'], 'done')
            resumed = make_daily(week=True, ticket=True, store=store)
            self.run_daily(resumed)
            self.assertFalse(resumed.calls)

    def test_account_finalize_waits_for_ticket(self):
        task = make_multi(total_consignment_ticket_enable=True)
        task._progress_key_of = lambda account: 'account'
        task.daily_conf.update_account_login_history = Mock()
        task.config.save = Mock()
        account = task._create_account_config(NS['ExtendedAccountInfo']())
        tasks = task._enabled_task_keys(account, True)
        with tempfile.TemporaryDirectory() as directory:
            store = ProgressStore('test', directory)
            task._progress = store
            for name in tasks:
                if name != 'consignment_ticket':
                    store.mark_task('account', name, 'done')
            self.assertFalse(task._finalize_account_progress(SimpleNamespace(character='test'), account))
            store.mark_task('account', 'consignment_ticket', 'done')
            self.assertTrue(task._finalize_account_progress(SimpleNamespace(character='test'), account))
            self.assertTrue(store.is_account_done('account'))

    def test_plain_completion_lists_ticket_independently(self):
        task = make_multi(total_consignment_ticket_enable=True, total_weekaward_enable=False)
        items = task._build_plain_items()
        self.assertIn('购买寄售券', items)
        self.assertNotIn('每周奖励', items)

    def test_ticket_only_account_reaches_daily_executor(self):
        task = make_multi(total_consignment_ticket_enable=True)
        task._retry_account_identity = None
        task._create_account_config = lambda account: make_daily().get_config().daily_alt_acc_config
        task._switch_to_account = Mock(return_value=True)
        task._execute_daily_tasks = Mock(return_value=True)
        task.emit_stat = Mock()
        account = SimpleNamespace(is_valid=lambda: True, account='test', character='test',
                                  svr='test', apple_or_android=True)
        self.assertTrue(task._process_single_account(account))
        task._switch_to_account.assert_called_once_with(account)
        task._execute_daily_tasks.assert_called_once()
        self.assertEqual(task.emit_stat.call_args.kwargs['tasks'], ['consignment_ticket'])

    def test_old_snapshot_upgrade_keeps_existing_task_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProgressStore('test', directory)
            flags = NS['phase_flags_of'](MultiConfig(), 'morning')
            old_flags = {k: v for k, v in flags.items() if k != 'total_consignment_ticket_enable'}
            store.ensure_phase(old_flags, 'original')
            store.mark_task('account', 'weekaward', 'done')
            store.mark_account_done('account')
            flags['total_consignment_ticket_enable'] = True
            self.assertFalse(store.ensure_phase(flags, 'new'))
            self.assertEqual(store.get_task('account', 'weekaward')['status'], 'done')
            self.assertFalse(store.is_task_finished('account', 'consignment_ticket'))
            self.assertTrue(store._data['phase_flags']['total_consignment_ticket_enable'])


if __name__ == '__main__':
    unittest.main()
