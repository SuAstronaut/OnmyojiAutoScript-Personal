"""Offline SixRealms completion-count regressions, independent of game logic.

Run: OAS_test_venv/Scripts/python.exe -B dev_tools/test_sixrealms_stats.py -v
No production logger, device, run state or business adapters are imported.
Only isolated TemporaryDirectory log fixtures are written.
"""
import ast
import importlib.util
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('six_stats_test', ROOT / 'module/server/log_stats.py')
stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stats
SPEC.loader.exec_module(stats)

# Extract response contracts without importing the server or its logger.
SCHEMA_NS = {'BaseModel': BaseModel, 'Field': Field}
schema_nodes = [node for node in ast.parse((ROOT / 'module/server/stats_router.py').read_text(encoding='utf-8')).body
                if isinstance(node, ast.ClassDef)]
exec(compile(ast.Module(body=schema_nodes, type_ignores=[]), 'stats_response_contract', 'exec'), SCHEMA_NS)


def line(seconds, message, day='2026-10-03', newline=True):
    stamp = datetime.fromisoformat(day) + timedelta(seconds=seconds)
    return stamp.strftime('%Y-%m-%d %H:%M:%S.000') + ' | moon_sea.py:0042 | INFO | ' + message + ('\n' if newline else '')


def segment(counts, offset=0):
    return ([line(offset, 'Scheduler: Start task `SixRealms`')]
            + [line(offset+i*60, f'Run {count} times,avg time: 0:01:00, total time: 0:03:00')
               for i,count in enumerate(counts,1)]
            + [line(offset+(len(counts)+1)*60, 'Scheduler: End task `SixRealms`')])


class StatsTests(unittest.TestCase):
    def six(self, lines): return stats.LogStatsParser.parse_lines(lines)['tasks']['SixRealms']
    def test_one_run(self): self.assertEqual(self.six(segment([1]))['completed_run_count'], 1)
    def test_cumulative_not_sum(self): self.assertEqual(self.six(segment([1,2,3]))['completed_run_count'], 3)
    def test_restart_segments(self):
        result = self.six(segment([1,2])+segment([1,2,3],1000))
        self.assertEqual(result['completed_run_count'],5)
        self.assertEqual([r['completed_run_count'] for r in result['runs']], [2,3])
        self.assertEqual(result['total_duration_seconds'],420)
    def test_last_unfinished_not_counted(self):
        self.assertEqual(self.six(segment([1,2]))['completed_run_count'],2)
    def test_abnormal_end_without_completion(self):
        self.assertEqual(self.six(segment([]))['completed_run_count'],0)
    def test_replayed_chunk_not_double_counted(self):
        parser=stats.LogStatsParser(); lines=segment([1,2])
        parser.consume_lines(lines); first=parser.snapshot()
        parser.consume_lines(lines)
        self.assertEqual(parser.snapshot(),first)
    def test_live_snapshots_do_not_consume_or_duplicate(self):
        parser=stats.LogStatsParser(); lines=segment([1,2,3])
        parser.consume_lines(lines[:2])
        self.assertEqual(parser.snapshot()['tasks']['SixRealms']['completed_run_count'],1)
        parser.snapshot(); parser.consume_lines(lines[2:])
        self.assertEqual(parser.snapshot()['tasks']['SixRealms']['completed_run_count'],3)
    def test_missing_start_reports_partial(self):
        result=stats.LogStatsParser.parse_lines(segment([1,2])[1:])
        self.assertNotIn('SixRealms',result['tasks'])
        self.assertTrue(result['statistics_warnings'])
    def test_midnight_without_start_is_explicitly_partial(self):
        result=stats.LogStatsParser.parse_lines([line(60,'Run 3 times,avg time: 0:01:00, total time: 0:03:00',day='2026-10-04')])
        self.assertTrue(result['statistics_warnings'])
    def test_truncated_completion_not_counted(self):
        lines=segment([])[:1]+[line(60,'Run 1 times,avg time: 0:01:00, total time: 0:03:00',newline=False)]
        self.assertEqual(self.six(lines)['completed_run_count'],0)
    def test_decrease_without_boundary_reports_partial(self):
        result=stats.LogStatsParser.parse_lines(segment([1,2,1]))
        self.assertTrue(result['statistics_warnings'])
        self.assertEqual(result['tasks']['SixRealms']['completed_run_count'],2)
    def test_diagnostic_completion_not_double_counted(self):
        lines=segment([1]); lines.insert(-1,line(70,'[STAT] {"ev":"sixrealms_run_complete"}'))
        self.assertEqual(self.six(lines)['completed_run_count'],1)
    def test_service_ignores_error_log_copy(self):
        with tempfile.TemporaryDirectory(prefix='six_stats_log_') as directory:
            root=Path(directory); text=''.join(segment([1,2]))
            (root/'2026-10-03_fixture.txt').write_text(text,encoding='utf-8')
            (root/'error'/'copy').mkdir(parents=True)
            (root/'error'/'copy'/'log.txt').write_text(text,encoding='utf-8')
            with patch.object(stats,'LOG_ROOT',root):
                payload=stats.LogStatsService()._build_historical_stats('fixture',date(2026,10,3))
            self.assertEqual(payload['tasks']['SixRealms']['completed_run_count'],2)
    def test_incremental_partial_line_completed_once(self):
        with tempfile.TemporaryDirectory(prefix='six_stats_tail_') as directory:
            root=Path(directory); path=root/f'{date.today().isoformat()}_fixture.txt'
            rows=segment([1]); service=stats.LogStatsService()
            with patch.object(stats,'LOG_ROOT',root):
                path.write_text(rows[0]+rows[1][:35],encoding='utf-8')
                first=service._build_today_stats('fixture')
                self.assertEqual(first['tasks']['SixRealms']['completed_run_count'],0)
                with path.open('a',encoding='utf-8') as file: file.write(rows[1][35:]+rows[2])
                second=service._build_today_stats('fixture')
                self.assertEqual(second['tasks']['SixRealms']['completed_run_count'],1)
                self.assertEqual(service._build_today_stats('fixture'),second)
    def test_warning_only_change_is_streamed(self):
        service=stats.LogStatsService(); before=service._empty_stats('fixture')
        after=dict(before,statistics_warnings=['partial'])
        self.assertEqual(service._build_update_payload('fixture',before,after)['statistics_warnings'],['partial'])
    def test_api_models_keep_completion_fields(self):
        payload=stats.LogStatsService._parse_lines(segment([1,2,3]),'fixture')
        model=SCHEMA_NS['StatsResponse'](**payload)
        self.assertEqual(model.tasks['SixRealms'].completed_run_count,3)
        self.assertEqual(model.tasks['SixRealms'].runs[0].completed_run_count,3)
        update=stats.LogStatsService()._build_update_payload('fixture',{},payload)
        self.assertEqual(SCHEMA_NS['StatsUpdateResponse'](**update).changed_tasks['SixRealms'].completed_run_count,3)
    def test_midnight_actions_before_completion_warn(self):
        payload=stats.LogStatsParser.parse_lines([line(10,'Skill power level: 8',day='2026-10-04')])
        self.assertTrue(payload['statistics_warnings'])


if __name__ == "__main__":
    unittest.main()
