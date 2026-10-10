"""Offline account-list recognition and login-boundary regressions."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import numpy as np

from test_takeover_reconnect import methods

ROOT = Path(__file__).resolve().parents[1]


class DropdownTests(unittest.TestCase):
    def task(self, *, logo=True, brightness=43, add=False):
        source = ast.parse((ROOT / 'tasks/Component/SwitchAccount/dropdown_state.py').read_text(encoding='utf-8'))
        source.body = [node for node in source.body if not isinstance(node, ast.ImportFrom)]
        assets = NS(I_SA_NETEASE_GAME_LOGO='logo', I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT='add')
        namespace = {'SwitchAccountAssets': assets}
        exec(compile(source, '<dropdown>', 'exec'), namespace)
        task = NS(device=NS(image=np.full((720, 1280, 3), brightness, dtype=np.uint8)))
        task.appear = Mock(side_effect=lambda rule: {'logo': logo, 'add': add}[rule])
        return task, namespace['is_account_dropdown_opened']

    def test_short_list_uses_add_account(self):
        task, opened = self.task(add=True)
        self.assertTrue(opened(task))

    def test_long_list_preserves_brightness_and_short_circuit(self):
        task, opened = self.task(brightness=220)
        self.assertTrue(opened(task))
        task.appear.assert_called_once_with('logo')

    def test_closed_list_is_false(self):
        task, opened = self.task()
        self.assertFalse(opened(task))

    def test_bright_non_account_page_is_false(self):
        task, opened = self.task(logo=False, brightness=254, add=True)
        self.assertFalse(opened(task))
        task.appear.assert_called_once_with('logo')

    def test_resources_search_above_bottom_strip(self):
        data = json.loads((ROOT / 'tasks/Component/SwitchAccount/res/image.json').read_text(encoding='utf-8'))
        entry = next(item for item in data if item['itemName'] == 'sa_account_drop_down_add_account')
        self.assertEqual(entry['roiBack'], '400,280,220,320')
        tree = ast.parse((ROOT / 'tasks/Component/SwitchAccount/assets.py').read_text(encoding='utf-8'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        call = next(node.value for node in cls.body if isinstance(node, ast.Assign)
                    and node.targets[0].id == 'I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT')
        roi = next(value.value for value in call.keywords if value.arg == 'roi_back')
        self.assertEqual(ast.literal_eval(roi), (400, 280, 220, 320))

    def test_restart_handoff_closes_short_list_before_accepting_form(self):
        task, opened = self.task(add=True)
        cls = methods(ROOT / 'tasks/Restart/login.py', 'LoginHandler', {'_prepare_login_handoff'},
                      {'SwitchAccountAssets': NS(C_SA_ACCOUNT_DROPDOWN_BLANK='blank'),
                       'is_account_dropdown_opened': opened})
        task.click = Mock()
        self.assertFalse(cls._prepare_login_handoff(task))
        task.click.assert_called_once_with('blank', interval=1)

    def test_desktop_and_reconnect_never_handoff_to_account_picker(self):
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask', {'_get_login_handoff_task'}, {})
        for desktop, reconnect in ((True, False), (False, True)):
            task = cls()
            task.device, task._reconnect_mode = NS(is_desktop=desktop), reconnect
            self.assertEqual(task._get_login_handoff_task(), '')


if __name__ == '__main__':
    unittest.main()
