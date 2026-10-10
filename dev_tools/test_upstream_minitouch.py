"""AST-loaded transport regressions. Every socket/ADB/ATX/U2/WS call is fake."""
import ast
import asyncio
import copy
from functools import cached_property, wraps
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from types import SimpleNamespace as NS
from typing import List
import unittest
from unittest.mock import AsyncMock, Mock

import numpy as np

from module.exception import RequestHumanTakeover
from test_takeover_reconnect import Clock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'module/device/method/minitouch.py'
READY = ['v 1\n', '^ 2 1280 720 50\n', '$ 11\n']


class Builder:
    DEFAULT_DELAY = 0.05
    delay = 0

    def __init__(self, device=None):
        self.clear = Mock()
    def down(self, *args, **kwargs): return self
    def up(self, *args, **kwargs): return self
    def commit(self): return self
    def to_minitouch(self): return 'd 0 10 10 50\nc\nu 0\nc\n'
    def to_atx_agent(self): return ['down', 'up']


class FakeSocket:
    def __init__(self, clock, lines=None, connect_error=None):
        self.clock, self.lines, self.connect_error = clock, iter(lines or []), connect_error
        self.closed, self.reader_closed, self.writes = False, False, []
    def connect(self, address):
        if self.connect_error:
            raise self.connect_error
    def settimeout(self, seconds): self.timeout = seconds
    def makefile(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): self.reader_closed = True
    def readline(self):
        line = next(self.lines, '')
        if isinstance(line, Exception): raise line
        if not line: self.clock.sleep(0.1)
        return line
    def close(self): self.closed = True
    def sendall(self, data): self.writes.append(data)
    def recv(self, size): return b''


class Harness:
    def __init__(self, http=False, source=None):
        self.clock, self.clients, self.created = Clock(), [], []
        fake_process = NS(run=Mock(return_value=NS(stdout='')), PIPE=subprocess.PIPE,
                          CalledProcessError=subprocess.CalledProcessError,
                          TimeoutExpired=subprocess.TimeoutExpired, CREATE_NO_WINDOW=0)
        fake_socket = NS(socket=self.socket, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout)
        ns = dict(asyncio=asyncio, json=json, re=re, socket=fake_socket, subprocess=fake_process,
                  sys=sys, np=np, List=List, wraps=wraps, logger=Mock(),
                  time=NS(monotonic=self.clock.time, time=self.clock.time, sleep=self.clock.sleep),
                  RequestHumanTakeover=RequestHumanTakeover, RETRY_TRIES=3,
                  retry_sleep=lambda attempt: None, handle_adb_error=lambda error: False,
                  AdbError=type('AdbError', (Exception,), {}),
                  WebSocketException=type('WebSocketException', (Exception,), {}),
                  u2=NS(_AgentRequestSession=type('Agent', (), {})),
                  Urllib3Timeout=Mock(), CommandBuilder=Builder,
                  random_port=lambda port_range: 40001,
                  del_cached_property=lambda obj, key: obj.__dict__.pop(key, None),
                  U2Service=lambda name, **kwargs: NS(service_url=kwargs['service_url'], _raise_for_status=Mock()),
                  humanized_http_request=Mock(), websockets=NS(connect=AsyncMock()))
        tree = ast.parse(source or SOURCE.read_text(encoding='utf-8'))
        selected = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                selected.append(copy.deepcopy(node))
            elif isinstance(node, ast.ClassDef) and node.name in {
                    '_MinitouchRecoveryFailed', '_MinitouchHandshakeNotReady',
                    'MinitouchNotInstalledError', 'MinitouchOccupiedError'}:
                selected.append(copy.deepcopy(node))
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith(('MINITOUCH_', '_MINITOUCH_')) or name == '_HUMANIZED_MINITOUCH_TRANSPORT_ERRORS':
                    selected.append(copy.deepcopy(node))
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), ns)
        cls = type('Minitouch', (), {})
        ns['Minitouch'] = cls
        source_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Minitouch')
        self.variants = {}
        counts = {}
        for original in source_cls.body:
            if not isinstance(original, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            node = copy.deepcopy(original)
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
            fn = ns[node.name]
            index = counts.get(node.name, 0)
            counts[node.name] = index + 1
            self.variants[(node.name, index)] = fn
            if index == 0:
                setattr(cls, node.name, fn)
        self.ns, self.device = ns, cls()
        d = self.device
        d.config = NS(DEVICE_OVER_HTTP=http, FORWARD_PORT_RANGE=(40000, 50000))
        d.serial, d.adb_binary = ('https://fake-device' if http else 'fake-serial'), 'mock-adb'
        d.max_x, d.max_y, d.max_pressure, d.orientation = 1280, 720, 50, 0
        d._minitouch_port, d._minitouch_pid, d._atx_agent_ensure_tried = 40000, '10', False
        d.minitouch_builder = Builder()
        d.sleep, d.get_orientation = self.clock.sleep, Mock()
        d.install_uiautomator2 = Mock()
        d.adb_reconnect = Mock()
        d.restart_atx = Mock()
        d.humanizer = NS(rng=None, gap_seconds=lambda value: 0, press_seconds=lambda: 0)
        d._minitouch_loop_run = asyncio.run

    def socket(self, *args):
        client = self.clients.pop(0) if self.clients else FakeSocket(self.clock)
        self.created.append(client)
        return client

    def tcp(self, sequences, *, old='10', restart=True):
        self.clients = [FakeSocket(self.clock, lines) for lines in sequences]
        self.device._ensure_humanized_minitouch_service_started = Mock()
        self.device._recover_humanized_minitouch_tcp(old, restart_atx=restart)


class EnvironmentTests(unittest.TestCase):
    def test_binaries_ready_caches_only_verified_state(self):
        h = Harness()
        h.ns['subprocess'].run.return_value.stdout = 'atx-agent ready\nminitouch ready\n'
        h.device._ensure_minitouch_environment()
        h.device._ensure_minitouch_environment()
        self.assertTrue(h.device._minitouch_environment_ready)
        self.assertEqual(h.ns['subprocess'].run.call_count, 1)
        h.device.install_uiautomator2.assert_not_called()

    def test_missing_binary_installs_and_verifies(self):
        h = Harness()
        h.ns['subprocess'].run.side_effect = [NS(stdout='atx-agent ready\nminitouch missing\n'),
                                             NS(stdout='atx-agent ready\nminitouch ready\n')]
        h.device._ensure_minitouch_environment()
        h.device.install_uiautomator2.assert_called_once()
        self.assertTrue(h.device._minitouch_environment_ready)

    def test_chmod_repairs_fixed_path_and_rechecks(self):
        h = Harness()
        h.ns['subprocess'].run.side_effect = [NS(stdout='atx-agent ready\nminitouch chmod\n'),
                                             NS(stdout=''), NS(stdout='atx-agent ready\nminitouch ready\n')]
        h.device._ensure_minitouch_environment()
        commands = [call.args[0] for call in h.ns['subprocess'].run.call_args_list]
        self.assertIn(['mock-adb', '-s', 'fake-serial', 'shell', 'chmod', '755', '/data/local/tmp/minitouch'], commands)
        self.assertTrue(all(call.kwargs['timeout'] <= 10 for call in h.ns['subprocess'].run.call_args_list))

    def test_query_failure_reconnects_only_once(self):
        h = Harness()
        h.ns['subprocess'].run.side_effect = [subprocess.TimeoutExpired('mock', 10),
                                             NS(stdout='atx-agent ready\nminitouch ready\n')]
        h.device._ensure_minitouch_environment()
        h.device.adb_reconnect.assert_called_once()

    def test_invalid_or_still_missing_environment_never_caches_success(self):
        for values in (['nonsense'], ['atx-agent ready\nminitouch missing\n'] * 2):
            with self.subTest(values=values):
                h = Harness()
                h.ns['subprocess'].run.side_effect = [NS(stdout=value) for value in values]
                with self.assertRaises(RequestHumanTakeover):
                    h.device._ensure_minitouch_environment()
                self.assertNotIn('_minitouch_environment_ready', h.device.__dict__)

    def test_second_transport_failure_stops_without_reinstall_loop(self):
        h = Harness()
        h.ns['subprocess'].run.side_effect = subprocess.TimeoutExpired('mock', 10)
        with self.assertRaises(RequestHumanTakeover): h.device._ensure_minitouch_environment()
        self.assertEqual(h.ns['subprocess'].run.call_count, 2)
        h.device.adb_reconnect.assert_called_once()
        h.device.install_uiautomator2.assert_not_called()

    def test_force_rechecks_and_http_never_calls_adb(self):
        h = Harness()
        h.device._minitouch_environment_ready = True
        h.ns['subprocess'].run.return_value.stdout = 'atx-agent ready\nminitouch ready\n'
        h.device._ensure_minitouch_environment(force=True)
        self.assertEqual(h.ns['subprocess'].run.call_count, 1)
        h = Harness(http=True)
        h.device._ensure_minitouch_environment(force=True)
        h.ns['subprocess'].run.assert_not_called()


class TcpTests(unittest.TestCase):
    def test_new_session_releases_old_client_and_handshake_reader(self):
        h = Harness()
        old = h.device._minitouch_client = Mock()
        h.tcp([READY])
        old.close.assert_called_once()
        self.assertTrue(h.created[0].reader_closed)
        self.assertFalse(h.created[0].closed)
        self.assertEqual(h.device._minitouch_pid, '11')
        stages = [call.args[0] for call in h.ns['subprocess'].run.call_args_list]
        self.assertEqual(sum('--stop' in command for command in stages), 1)
        self.assertEqual(sum('--nouia' in command for command in stages), 1)

    def test_eof_then_delayed_handshake_ensures_service_once(self):
        h = Harness()
        h.tcp([[], [socket.timeout('not ready')], [], READY])
        h.device._ensure_humanized_minitouch_service_started.assert_called_once()
        self.assertTrue(all(client.closed and client.reader_closed for client in h.created[:-1]))
        self.assertTrue(h.created[-1].reader_closed)

    def test_repeated_not_ready_is_bounded(self):
        h = Harness()
        with self.assertRaises(h.ns['_MinitouchRecoveryFailed']): h.tcp([[]])
        self.assertLess(h.clock.now, 120)
        h.device._ensure_humanized_minitouch_service_started.assert_called_once()

    def test_old_pid_and_invalid_protocol_are_rejected_without_service_restart(self):
        for lines in (['v 1\n', '^ 2 1280 720 50\n', '$ 10\n'],
                      ['invalid\n', READY[1], READY[2]], [READY[0], '^ 0 1280 720 50\n', READY[2]]):
            with self.subTest(lines=lines):
                h = Harness()
                with self.assertRaises(h.ns['_MinitouchRecoveryFailed']): h.tcp([lines])
                h.device._ensure_humanized_minitouch_service_started.assert_not_called()
                self.assertTrue(h.created[0].closed and h.created[0].reader_closed)

    def test_normal_off_initialization_has_full_handshake_without_atx_reset(self):
        h = Harness()
        h.device._ensure_minitouch_environment = Mock()
        h.clients = [FakeSocket(h.clock, READY)]
        h.device.minitouch_init()
        h.device._ensure_minitouch_environment.assert_called_once()
        self.assertTrue(h.created[0].reader_closed)
        self.assertEqual(h.device._minitouch_pid, '11')
        commands = [call.args[0] for call in h.ns['subprocess'].run.call_args_list]
        self.assertFalse(any('--stop' in command or '--nouia' in command for command in commands))


    def test_off_first_connection_atx_unavailable_bootstraps_once_before_retry(self):
        h = Harness()
        h.device._ensure_minitouch_environment = Mock()
        h.clients = [FakeSocket(h.clock), FakeSocket(h.clock, READY)]
        h.device._ensure_humanized_minitouch_service_started = Mock(
            side_effect=h.ns['_MinitouchRecoveryFailed']('ATX unavailable'))
        h.device.minitouch_init()
        h.device.restart_atx.assert_called_once()
        self.assertTrue(h.device._atx_agent_ensure_tried)
        self.assertEqual(h.device._minitouch_pid, '11')
        self.assertTrue(h.created[0].closed and h.created[0].reader_closed)
        self.assertTrue(h.created[1].reader_closed)
        h.device._ensure_minitouch_environment.assert_any_call(force=True)
        self.assertFalse(any(client.writes for client in h.created))

    def test_off_first_connection_second_failure_stops_without_second_bootstrap(self):
        h = Harness()
        h.device._ensure_minitouch_environment = Mock()
        h.clients = [FakeSocket(h.clock), FakeSocket(h.clock)]
        h.device._ensure_humanized_minitouch_service_started = Mock(
            side_effect=h.ns['_MinitouchRecoveryFailed']('ATX unavailable'))
        with self.assertRaisesRegex(RequestHumanTakeover, 'after ATX bootstrap'):
            h.device.minitouch_init()
        h.device.restart_atx.assert_called_once()
        self.assertEqual(len(h.created), 2)
        self.assertTrue(all(client.closed and client.reader_closed for client in h.created))
        self.assertFalse(any(client.writes for client in h.created))

    def test_pressure_range_zero_and_actual_device_range_are_preserved(self):
        for pressure in (0, 50, 100):
            with self.subTest(pressure=pressure):
                h = Harness()
                h.tcp([['v 1\n', f'^ 2 1280 720 {pressure}\n', '$ 11\n']])
                self.assertEqual(h.device.max_pressure, pressure)


class ServiceTests(unittest.TestCase):
    def test_running_unusable_or_unknown_service_stops_before_start(self):
        for running in (True, None, 'invalid'):
            with self.subTest(running=running):
                h = Harness()
                h.device._humanized_service_request = Mock(return_value=NS(json=lambda: {'running': running}))
                h.device._wait_humanized_service_state = Mock()
                h.device._ensure_humanized_minitouch_service_started(110)
                self.assertEqual([call.args[1] for call in h.device._humanized_service_request.call_args_list], ['get', 'delete', 'post'])
                self.assertEqual([call.args[1] for call in h.device._wait_humanized_service_state.call_args_list], [False, True])
                self.assertIn('--remove', h.ns['subprocess'].run.call_args.args[0])

    def test_stopped_service_starts_without_duplicate_stop(self):
        h = Harness()
        h.device._humanized_service_request = Mock(return_value=NS(json=lambda: {'running': False}))
        h.device._wait_humanized_service_state = Mock()
        h.device._ensure_humanized_minitouch_service_started(110)
        self.assertEqual([call.args[1] for call in h.device._humanized_service_request.call_args_list], ['get', 'post'])

    def test_state_polling_is_bounded_and_invalid_state_is_rejected(self):
        for running in (False, None):
            with self.subTest(running=running):
                h = Harness()
                h.device._humanized_service_request = Mock(return_value=NS(json=lambda: {'running': running}))
                with self.assertRaises(h.ns['_MinitouchRecoveryFailed']):
                    h.device._wait_humanized_service_state(NS(), True, 100.2)
                self.assertLessEqual(h.clock.now, 100.2)

    def test_uiautomator_requests_disable_implicit_retry(self):
        h = Harness()
        response = NS()
        http = NS(get=Mock(return_value=response))
        h.device.u2 = NS(http=http)
        service = NS(service_url='http://fake/services/minitouch', _raise_for_status=Mock())
        self.assertIs(h.device._humanized_service_request(service, 'get', 110), response)
        self.assertFalse(http.get.call_args.kwargs['retry'])
        self.assertIsInstance(http.get.call_args.kwargs['timeout'], tuple)

    def test_http_recovery_observes_stop_and_start_without_adb(self):
        h = Harness(http=True)
        h.device._humanized_service_request = Mock()
        h.device._wait_humanized_service_state = Mock()
        async def ready(): pass
        h.device._recover_humanized_minitouch_http_async = ready
        h.device._recover_humanized_minitouch_http()
        self.assertEqual([call.args[1] for call in h.device._humanized_service_request.call_args_list], ['delete', 'post'])
        self.assertEqual([call.args[1] for call in h.device._wait_humanized_service_state.call_args_list], [False, True])
        h.ns['subprocess'].run.assert_not_called()
        h.device.restart_atx.assert_not_called()


    def test_http_b1_closes_old_ws_once_before_replacing_session(self):
        h = Harness(http=True)
        events = []
        async def close_old(): events.append('close-old')
        old = h.device._minitouch_ws = NS(close=AsyncMock(side_effect=close_old))
        h.device._humanized_service_request = Mock(side_effect=lambda service, method, deadline: events.append(method))
        h.device._wait_humanized_service_state = Mock()
        async def ready():
            events.append('ready-new')
            h.device._minitouch_ws = NS()
        h.device._recover_humanized_minitouch_http_async = ready
        h.device.recover_humanized_minitouch_b1()
        old.close.assert_awaited_once()
        self.assertEqual(events, ['close-old', 'delete', 'post', 'ready-new'])
        self.assertIsNot(h.device._minitouch_ws, old)

    def test_https_keeps_wss_and_bad_ready_closes_new_connection(self):
        for messages, valid in ((['ready', 'ready'], True), (['ready', ''], False), ([b'ready', 'ready'], False)):
            with self.subTest(messages=messages):
                h = Harness(http=True)
                ws = NS(recv=AsyncMock(side_effect=messages), close=AsyncMock())
                h.ns['websockets'].connect.return_value = ws
                if valid:
                    asyncio.run(h.device._recover_humanized_minitouch_http_async())
                    self.assertIs(h.device._minitouch_ws, ws)
                else:
                    with self.assertRaises(h.ns['_MinitouchRecoveryFailed']):
                        asyncio.run(h.device._recover_humanized_minitouch_http_async())
                    ws.close.assert_awaited_once()
                h.ns['websockets'].connect.assert_called_once_with('wss://fake-device/minitouch')


    def test_off_http_initializer_releases_old_ws_and_uses_bounded_service_flow(self):
        h = Harness(http=True)
        old = h.device._minitouch_ws = NS(close=AsyncMock())
        h.device._prepare_humanized_orientation = Mock()
        h.device._humanized_service_request = Mock()
        h.device._wait_humanized_service_state = Mock()
        async def ready(): pass
        h.device._recover_humanized_minitouch_http_async = ready
        h.variants[('minitouch_init', 1)](h.device)
        old.close.assert_awaited_once()
        self.assertEqual([call.args[1] for call in h.device._humanized_service_request.call_args_list],
                         ['delete', 'post'])
        self.assertEqual([call.args[1] for call in h.device._wait_humanized_service_state.call_args_list],
                         [False, True])
        h.ns['subprocess'].run.assert_not_called()
        h.device.restart_atx.assert_not_called()

    def test_off_http_initializer_failure_never_proceeds_to_input(self):
        h = Harness(http=True)
        h.device._prepare_humanized_orientation = Mock()
        h.device._recover_humanized_minitouch_http = Mock(
            side_effect=h.ns['_MinitouchRecoveryFailed']('stop deadline expired'))
        with self.assertRaisesRegex(RequestHumanTakeover, 'HTTP initialization failed'):
            h.variants[('minitouch_init', 1)](h.device)
        self.assertNotIn('_minitouch_ws', h.device.__dict__)

    def test_ws_ready_timeout_closes_new_connection(self):
        h = Harness(http=True)
        h.ns['MINITOUCH_RECOVERY_WS_READY_TIMEOUT_S'] = .01
        async def blocked(): await asyncio.sleep(10)
        ws = NS(recv=AsyncMock(side_effect=blocked), close=AsyncMock())
        h.ns['websockets'].connect.return_value = ws
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(h.device._recover_humanized_minitouch_http_async())
        ws.close.assert_awaited_once()
        self.assertNotIn('_minitouch_ws', h.device.__dict__)


class ReplayTests(unittest.TestCase):
    def test_success_and_b0_do_not_change_normal_dispatch(self):
        h = Harness()
        legacy = Mock()
        h.device.minitouch_send = Mock()
        h.device.recover_humanized_minitouch_b1 = Mock()
        h.device._run_humanized_minitouch(lambda builder, send: send(), legacy)
        h.device.recover_humanized_minitouch_b1.assert_not_called()
        legacy.assert_not_called()
        def fail_before_write(builder, send): raise OSError('before write')
        h.device._run_humanized_minitouch(fail_before_write, legacy)
        legacy.assert_called_once()

    def test_successful_b1_never_replays_even_when_context_looks_valid(self):
        h = Harness()
        legacy = Mock()
        h.device.minitouch_send = Mock(side_effect=BrokenPipeError('unknown result'))
        h.device.recover_humanized_minitouch_b1 = Mock()
        with self.assertRaisesRegex(RequestHumanTakeover, 'replay refused'):
            h.device._run_humanized_minitouch(lambda builder, send: send(), legacy)
        legacy.assert_not_called()
        h.device.recover_humanized_minitouch_b1.assert_called_once()

    def test_stop_disable_frame_task_device_or_window_change_never_replays(self):
        mutations = ('stop', 'disabled', 'frame', 'task', 'session', 'window', 'emulator')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                h = Harness()
                h.device.context = NS(stop=False, enabled=True, frame='old', task='original',
                                      session='old', window='old', emulator='alive')
                def recover():
                    setattr(h.device.context, {'disabled': 'enabled'}.get(mutation, mutation),
                            False if mutation == 'disabled' else 'changed')
                h.device.recover_humanized_minitouch_b1 = Mock(side_effect=recover)
                h.device.minitouch_send = Mock(side_effect=OSError('unknown result'))
                legacy = Mock()
                with self.assertRaises(RequestHumanTakeover):
                    h.device._run_humanized_minitouch(lambda builder, send: send(), legacy)
                legacy.assert_not_called()

    def test_failed_b1_never_replays(self):
        h = Harness()
        h.device.minitouch_send = Mock(side_effect=BrokenPipeError())
        h.device.recover_humanized_minitouch_b1 = Mock(side_effect=RequestHumanTakeover('reset failed'))
        legacy = Mock()
        with self.assertRaisesRegex(RequestHumanTakeover, 'reset failed'):
            h.device._run_humanized_minitouch(lambda builder, send: send(), legacy)
        legacy.assert_not_called()

    def test_off_tcp_and_http_write_failures_are_not_retried(self):
        for http in (False, True):
            with self.subTest(http=http):
                h = Harness(http=http)
                d = h.device
                d.recover_humanized_minitouch_b1 = Mock()
                if http:
                    d._minitouch_ws = NS(send=AsyncMock(side_effect=OSError('unknown write')))
                    d.minitouch_send = h.variants[('minitouch_send', 1)].__get__(d)
                else:
                    d._minitouch_client = NS(sendall=Mock(side_effect=OSError('unknown write')))
                fn = h.ns['retry'](d._click_minitouch_legacy_impl.__func__)
                with self.assertRaises(RequestHumanTakeover): fn(d, 10, 10)
                d.recover_humanized_minitouch_b1.assert_called_once()
                if http: d._minitouch_ws.send.assert_awaited_once()
                else: d._minitouch_client.sendall.assert_called_once()
                self.assertNotIn('_legacy_minitouch_transport_started', d.__dict__)

    def test_off_success_preserves_payload_and_has_no_recovery(self):
        h = Harness()
        h.device._minitouch_client = FakeSocket(h.clock, READY)
        h.device.recover_humanized_minitouch_b1 = Mock()
        h.ns['retry'](h.device._click_minitouch_legacy_impl.__func__)(h.device, 10, 10)
        self.assertEqual(len(h.device._minitouch_client.writes), 1)
        h.device.recover_humanized_minitouch_b1.assert_not_called()

    def test_control_gate_interrupt_does_not_enter_backend(self):
        from test_android_login_stages import MuMuFixture
        from tasks.Restart.reconnect import ReconnectInterrupted
        f = MuMuFixture()
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'WAIT_AT_GAME_LOGIN'
        before = (f.gate.deadline, f.gate.attempts)
        with self.assertRaises(ReconnectInterrupted): f.device.click(630, 595, pace=False)
        f.device.click_adb.assert_not_called()
        self.assertEqual((f.gate.deadline, f.gate.attempts), before)

    def test_game_app_prepare_does_not_invoke_minitouch_session_recovery(self):
        from test_android_login_stages import MuMuFixture
        f = MuMuFixture()
        f.device.recover_humanized_minitouch_b1 = Mock()
        f.device.restart_atx = Mock()
        f.confirm()
        f.recover()
        f.device.recover_humanized_minitouch_b1.assert_not_called()
        f.device.restart_atx.assert_not_called()


class BackendGateIntegrationTests(unittest.TestCase):
    """Real Script -> Restart/Login -> Control/Gate -> AST-loaded minitouch."""

    def connect_backend(self, f, h, humanized):
        h.clock = f.clock
        h.ns['time'] = NS(monotonic=f.clock.time, time=f.clock.time, sleep=f.clock.sleep)
        h.device.sleep = f.clock.sleep
        d = h.device
        if d.config.DEVICE_OVER_HTTP:
            d._minitouch_ws = NS(send=AsyncMock(side_effect=OSError('write result unknown')))
            d.minitouch_send = h.variants[('minitouch_send', 1)].__get__(d)
        else:
            d._minitouch_client = NS(sendall=Mock(side_effect=OSError('write result unknown')))
        legacy = Mock(side_effect=AssertionError('Old gesture replayed'))
        if humanized:
            backend = lambda x, y: d._run_humanized_minitouch(lambda builder, send: send(), legacy)
        else:
            fn = h.ns['retry'](d._click_minitouch_legacy_impl.__func__)
            backend = lambda x, y: fn(d, x, y)
        f.config.script.device.control_method = 'minitouch'
        f.device._humanizer_enabled = lambda: humanized
        f.device.humanizer = NS(pace_view=Mock())
        f.device.click_methods['minitouch'] = backend
        f.device.humanized_click_methods = {'minitouch': backend}
        return legacy

    def test_login_b1_context_changes_do_not_record_entry_extend_deadline_or_retry(self):
        from test_android_login_stages import MuMuFixture
        for http in (False, True):
            for humanized in (False, True):
                for mutation in ('stop', 'disabled', 'frame', 'task', 'session', 'unchanged', 'failed'):
                    with self.subTest(http=http, humanized=humanized, mutation=mutation):
                        f, h = MuMuFixture(cost=0), Harness(http=http)
                        legacy = self.connect_backend(f, h, humanized)
                        f.confirm()
                        deadline = f.gate.deadline
                        def recover():
                            self.assertEqual(f.gate.state, 'RECONNECTING')
                            self.assertEqual(f.gate.attempts, 1)
                            if mutation == 'stop': f.script.stop_event.set()
                            elif mutation == 'disabled':
                                f.config.restart.auto_reconnect_config.auto_reconnect_enable = False
                            elif mutation == 'frame': f.phase = 'unknown'
                            elif mutation == 'task': f.config.pending_task.clear()
                            elif mutation == 'session': h.device._minitouch_pid = 'session-replaced'
                            elif mutation == 'failed': raise RequestHumanTakeover('simulated B1 failure')
                        h.device.recover_humanized_minitouch_b1 = Mock(side_effect=recover)
                        with self.assertRaises(RequestHumanTakeover): f.recover()
                        self.assertEqual(f.gate.state, 'FAILED')
                        self.assertEqual(f.gate.deadline, deadline)
                        self.assertEqual(f.gate.attempts, 1)
                        self.assertEqual(f.gate.MAX_ATTEMPTS, 2)
                        self.assertIsNone(f.gate.game_entry_at)
                        self.assertFalse(f.entries())
                        legacy.assert_not_called()
                        f.device.click_adb.assert_not_called()
                        h.device.recover_humanized_minitouch_b1.assert_called_once()
                        self.assertEqual(sum(kind == 'app_stop' for kind, _ in f.events), 1)
                        self.assertEqual(sum(kind == 'app_start' for kind, _ in f.events), 1)
                        f.module.ScriptTask.assert_called_once()
                        f.device.full_recovery.assert_not_called()
                        if http: h.device._minitouch_ws.send.assert_awaited_once()
                        else: h.device._minitouch_client.sendall.assert_called_once()

    def test_off_ws_write_timeout_recovers_once_and_never_replays(self):
        h = Harness(http=True)
        h.ns['MINITOUCH_WS_SEND_TIMEOUT_S'] = .01
        async def blocked(row): await asyncio.sleep(10)
        h.device._minitouch_ws = NS(send=AsyncMock(side_effect=blocked))
        h.device.minitouch_send = h.variants[('minitouch_send', 1)].__get__(h.device)
        h.device.recover_humanized_minitouch_b1 = Mock()
        fn = h.ns['retry'](h.device._click_minitouch_legacy_impl.__func__)
        with self.assertRaises(RequestHumanTakeover): fn(h.device, 10, 10)
        h.device._minitouch_ws.send.assert_awaited_once()
        h.device.recover_humanized_minitouch_b1.assert_called_once()

    def test_restored_task_b1_takeover_releases_single_claim_without_normal_restart(self):
        from test_android_reconnect_safety import SchedulerLoopTests
        test = SchedulerLoopTests()
        self.addCleanup(test.doCleanups)
        h = Harness()
        legacy = Mock(side_effect=AssertionError('Old gesture replayed'))
        h.device.minitouch_send = Mock(side_effect=BrokenPipeError('unknown outcome'))
        h.device.recover_humanized_minitouch_b1 = Mock()
        def fail_in_backend(message):
            return h.device._run_humanized_minitouch(lambda builder, send: send(), legacy)
        f, calls = test.fixture(fail_in_backend)
        f.config.task_call = Mock(side_effect=AssertionError('Ordinary Restart scheduled'))
        f.script._run_task.__func__.__globals__['I18n'] = NS(trans_zh_cn=lambda command: command)
        deadline = f.gate.deadline
        with self.assertRaises(SystemExit) as caught:
            test.run_loop(f)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual([kind for kind, _ in calls], ['construct', 'run'])
        self.assertEqual(f.config.store.claim_scheduler_task.call_count, 1)
        self.assertEqual(f.config.store.release_scheduler_task.call_count, 1)
        self.assertEqual(f.config.actual_running_task, '')
        self.assertEqual(f.gate.deadline, deadline)
        self.assertEqual(f.gate.attempts, 1)
        f.config.task_call.assert_not_called()
        f.script.get_next_task.assert_not_called()
        h.device.recover_humanized_minitouch_b1.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(f.config.notifier.push.call_count, 1)


if __name__ == '__main__':
    unittest.main()
