"""Offline execution-time regressions; no logger, devices or production writes.

Run: OAS_test_venv/Scripts/python.exe -B dev_tools/test_log_stats_execution_time.py -v
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
# module.server.__init__ initializes the production logger. Load this pure
# parser/service module directly so tests cannot append to real instance logs.
SPEC = importlib.util.spec_from_file_location("execution_stats_under_test", ROOT / "module/server/log_stats.py")
stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stats
SPEC.loader.exec_module(stats)


def log(at, message):
    return f"2026-10-03 {at} | script.py:0695 | INFO | {message}\n"


def boundary(title):
    return ["═" * 80 + "\n", "─" * 30 + f" {title} " + "─" * 30 + "\n", "═" * 80 + "\n"]


def battle(at, end):
    return ["─" * 30 + " GENERAL BATTLE START " + "─" * 30 + "\n",
            log(at, "GENERAL BATTLE START"), log(end, "Battle result is win")]


class ExecutionTimeTests(unittest.TestCase):
    def parse(self, lines):
        return stats.LogStatsParser.parse_lines(lines, "test")

    def run_record(self, payload, task="RyouToppa", index=0):
        return payload["tasks"][task]["runs"][index]

    def queued_run(self, queued, started, finished):
        return [log(queued, "Pending tasks: ['RyouToppa']"),
                log(queued, "[Task] RyouToppa (Enable, 5, 2026-10-03 02:37:00)"),
                log(started, "Scheduler: Start task `RyouToppa`")] + boundary("RYOUTOPPA") + [
                    log(started, "UI recognition"), log(finished, "Scheduler: End task `RyouToppa`")]

    def test_long_queue_is_six_minutes(self):
        payload = self.parse(self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000"))
        record = self.run_record(payload)
        self.assertEqual(record["start_time"], "2026-10-03 19:35:00.000")
        self.assertEqual(record["end_time"], "2026-10-03 19:41:00.000")
        self.assertEqual(record["duration_seconds"], 360)
        self.assertEqual(payload["tasks"]["RyouToppa"]["total_duration_seconds"], 360)
        self.assertEqual(payload["total_runtime_seconds"], 360)

    def test_immediate_dispatch(self):
        payload = self.parse(self.queued_run("19:35:00.000", "19:35:00.005", "19:35:10.005"))
        self.assertEqual(self.run_record(payload)["duration_seconds"], 10)

    def test_repeated_pending_updates_do_not_open_execution(self):
        lines = [log("02:37:00.000", "[Task] RyouToppa (Enable, 5, 2023-01-01 00:00:00)"),
                 log("10:00:00.000", "Pending tasks: ['RyouToppa']"),
                 log("19:34:59.000", "[Task] RyouToppa (Enable, 5, 2023-01-01 00:00:00)")]
        payload = self.parse(lines + self.queued_run("19:35:00.000", "19:35:00.000", "19:41:00.000"))
        self.assertEqual(payload["total_task_run_count"], 1)
        self.assertEqual(self.run_record(payload)["duration_seconds"], 360)

    def test_never_started_has_no_run(self):
        payload = self.parse([log("02:37:00.000", "[Task] RyouToppa (Enable, 5, 2023-01-01 00:00:00)"),
                              log("19:41:00.000", "Wait until 2026-10-04 21:00:00 for task `RyouToppa`")])
        self.assertEqual(payload["tasks"], {})
        self.assertEqual(payload["total_runtime_seconds"], 0)

    def test_recoverable_failure_ends_at_scheduler_end(self):
        payload = self.parse(self.queued_run("02:37:00.000", "19:35:00.000", "19:35:10.000")[:-1] + [
            log("19:35:09.000", "Game not running"),
            log("19:35:10.000", "Scheduler: End task `RyouToppa`"),
            log("23:00:00.000", "[Task] Restart (Enable, 1, 2023-01-01 00:00:00)")])
        self.assertEqual(self.run_record(payload)["duration_seconds"], 10)

    def test_fatal_failure_ends_at_process_exit(self):
        lines = self.queued_run("02:37:00.000", "19:35:00.000", "19:35:10.000")[:-1]
        lines += [log("19:35:09.000", "Request human takeover"),
                  log("19:35:10.000", "Script test process exit"),
                  log("23:00:00.000", "Error: 1")]
        self.assertEqual(self.run_record(self.parse(lines))["duration_seconds"], 10)

    def test_zero_seconds_without_battles(self):
        payload = self.parse(self.queued_run("02:37:00.000", "02:37:50.000", "02:37:50.000"))
        record = self.run_record(payload)
        self.assertEqual(record["duration_seconds"], 0)
        self.assertIsNone(record["battle"])
        self.assertEqual(payload["total_task_run_count"], 1)

    def test_legacy_title_without_dispatch_is_not_a_start(self):
        lines = [log("02:37:00.000", "[Task] RyouToppa (Enable, 5, 2023-01-01 00:00:00)")]
        lines += boundary("RYOUTOPPA") + [log("02:37:54.975", "UI recognition"),
                                         log("19:41:24.431", "Scheduler: End task `RyouToppa`")]
        self.assertEqual(self.parse(lines)["tasks"], {})

    def test_existing_dispatch_logs_need_no_new_started_at_field(self):
        self.assertEqual(self.run_record(self.parse(self.queued_run(
            "02:37:00.000", "19:35:00.000", "19:41:00.000")))["duration_seconds"], 360)

    def test_interrupted_run_does_not_absorb_restart_diagnostics(self):
        # Exact timestamps from the reported 02:37:54 -> 19:41:24 incident.
        lines = [log("02:37:54.777", "Scheduler: Start task `RyouToppa`")]
        lines += boundary("RYOUTOPPA") + [log("02:37:54.975", "module_path: tasks/RyouToppa/script_task.py")]
        start = datetime(2026, 10, 3, 2, 38)
        for index in range(32):
            at = start + timedelta(seconds=20 * index)
            lines += battle(at.strftime("%H:%M:%S.%f")[:-3],
                            (at + timedelta(seconds=11)).strftime("%H:%M:%S.%f")[:-3])
        lines += [log("02:49:17.502", "Click @ GB_WIN_2"),
                  log("19:41:23.902", "[START_DIAG] script_import_begin config='test'"),
                  log("19:41:24.431", "[START_DIAG] script_import_done config='test'")]
        lines += boundary("START") + [log("19:41:24.433", "[START_DIAG] script_construct_done config='test'")]
        payload = self.parse(lines)
        record = self.run_record(payload)
        self.assertEqual(record["end_time"], "2026-10-03 02:49:17.502")
        self.assertEqual(record["duration_seconds"], 682.725)
        self.assertEqual(record["battle"], {"count": 32, "avg_duration_seconds": 11})
        self.assertEqual(payload["total_runtime_seconds"], 682.725)

    def test_finished_run_does_not_absorb_next_day_wait(self):
        lines = self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000")
        lines += [log("19:41:00.010", "[Task] RyouToppa (Enable, 5, 2026-10-04 21:00:00)"),
                  log("19:41:00.020", "Wait until 2026-10-04 21:00:00 for task `RyouToppa`"),
                  log("23:00:00.000", "Still waiting")]
        self.assertEqual(self.run_record(self.parse(lines))["duration_seconds"], 360)

    def test_multiple_dispatches_accumulate_only_execution(self):
        lines = self.queued_run("02:37:00.000", "03:00:00.000", "03:06:00.000")
        lines += self.queued_run("03:06:00.010", "19:35:00.000", "19:41:00.000")
        payload = self.parse(lines)
        task = payload["tasks"]["RyouToppa"]
        self.assertEqual(task["run_count"], 2)
        self.assertEqual(task["total_duration_seconds"], 720)
        self.assertEqual(payload["total_runtime_seconds"], 720)

    def test_task_internal_wait_is_execution(self):
        lines = self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000")
        lines.insert(-1, log("19:40:00.000", "Waiting for page_main"))
        self.assertEqual(self.run_record(self.parse(lines))["duration_seconds"], 360)

    def test_new_task_without_previous_end_uses_previous_last_evidence(self):
        lines = self.queued_run("02:37:00.000", "03:00:00.000", "03:06:00.000")[:-1]
        lines += [log("03:06:00.000", "last task action")]
        lines += self.queued_run("19:34:00.000", "19:35:00.000", "19:41:00.000")
        payload = self.parse(lines)
        self.assertEqual(self.run_record(payload)["duration_seconds"], 360)
        self.assertEqual(self.run_record(payload, index=1)["duration_seconds"], 360)

    def test_restart_title_without_diagnostics_does_not_start_legacy_run(self):
        lines = self.queued_run("02:37:00.000", "03:00:00.000", "03:06:00.000")[:-1]
        lines += [log("03:06:00.000", "last task action")] + boundary("START")
        lines += [log("19:41:00.000", "Start scheduler loop: test")]
        self.assertEqual(self.run_record(self.parse(lines))["duration_seconds"], 360)

    def test_live_snapshots_are_non_mutating_and_exclude_idle(self):
        parser = stats.LogStatsParser()
        parser.consume_lines(self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000")[:-1])
        parser.consume_lines([log("19:36:00.000", "task progress")])
        self.assertEqual(self.run_record(parser.snapshot())["duration_seconds"], 60)
        self.assertEqual(parser.summary, {})
        parser.consume_lines([log("19:41:00.000", "Scheduler: End task `RyouToppa`")])
        self.assertEqual(self.run_record(parser.snapshot())["duration_seconds"], 360)
        self.assertEqual(parser.snapshot()["total_task_run_count"], 1)

    def test_history_and_incremental_cache_use_same_intervals(self):
        with tempfile.TemporaryDirectory(prefix="oas_execution_stats_") as directory:
            root = Path(directory)
            history = root / "2026-10-03_test.txt"
            lines = self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000")
            original = "".join(lines).encode("utf-8")
            history.write_bytes(original)
            with patch.object(stats, "LOG_ROOT", root):
                service = stats.LogStatsService()
                historical = service._build_historical_stats("test", date(2026, 10, 3))
                self.assertEqual(self.run_record(historical)["duration_seconds"], 360)
                self.assertEqual(history.read_bytes(), original)
                today = root / f"{date.today().isoformat()}_live.txt"
                # Split across the decorative header.
                cuts = [4, 5, len(lines) - 1]
                previous = 0
                with today.open("w", encoding="utf-8", newline="") as file:
                    for cut in cuts:
                        file.write("".join(lines[previous:cut]))
                        file.flush()
                        live = service._build_today_stats("live")
                        previous = cut
                    end = lines[-1]
                    file.write(end[:len(end) // 2])
                    file.flush()
                    partial = service._build_today_stats("live")
                    self.assertEqual(partial["total_runtime_seconds"], 0)
                    file.write(end[len(end) // 2:])
                    file.flush()
                    live = service._build_today_stats("live")
                self.assertEqual(live["tasks"], historical["tasks"])
                self.assertEqual(live["total_runtime_seconds"], 360)
                self.assertEqual(service._build_today_stats("live"), live)

    def test_incremental_restart_truncates_before_new_process_timestamp(self):
        parser = stats.LogStatsParser()
        parser.consume_lines(self.queued_run("02:37:00.000", "03:00:00.000", "03:06:00.000")[:-1])
        parser.consume_lines([log("03:06:00.000", "last task action")])
        before = parser.snapshot()
        parser.consume_lines([log("19:41:24.431", "[START_DIAG] script_import_done config='test'")])
        after = parser.snapshot()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["total_runtime_seconds"], 360)

    def test_new_dispatch_without_queue_or_end_starts_a_separate_interval(self):
        lines = [log("03:00:00.000", "Scheduler: Start task `RyouToppa`"),
                 log("03:06:00.000", "last task action"),
                 log("19:35:00.000", "Scheduler: Start task `RyouToppa`"),
                 log("19:41:00.000", "Scheduler: End task `RyouToppa`")]
        task = self.parse(lines)["tasks"]["RyouToppa"]
        self.assertEqual([run["duration_seconds"] for run in task["runs"]], [360, 360])

    def test_unmatched_end_cannot_finish_another_task(self):
        lines = self.queued_run("02:37:00.000", "19:35:00.000", "19:41:00.000")
        lines.insert(-1, log("19:36:00.000", "Scheduler: End task `RealmRaid`"))
        self.assertEqual(self.run_record(self.parse(lines))["duration_seconds"], 360)


if __name__ == "__main__":
    unittest.main()
