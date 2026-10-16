"""Offline only: AST-load production methods, never import Device or account config.

Run from the repository root with toolkit/python.exe -B -m unittest
dev_tools.test_mshop_hotfix -v. Private real screenshots are
optional inputs; they are never copied into this repository or a release.
"""
import ast
from functools import cached_property, lru_cache
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import inspect
import itertools
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPORT = {"recognition": [], "flow": [], "compatibility": {
    "ldplayer": "RAW_DISABLED_AND_LIMIT_ONLY", "live": "NOT_RUN"}, "button": []}


def cases(names, values):
    def decorate(function):
        function.parameters = getattr(function, 'parameters', []) + [(names.split(','), values)]
        return function
    return decorate


class OfflineCase(unittest.TestCase):
    def __init__(self, function=None, kwargs=None):
        super().__init__('runTest')
        self.function = function if callable(function) else lambda: None
        self.kwargs = kwargs or {}
    def id(self):
        return self.function.__name__ + '[' + ','.join(f'{k}={v}' for k,v in self.kwargs.items()) + ']'
    def shortDescription(self):
        return self.id()
    def runTest(self):
        kwargs = dict(self.kwargs)
        if 'model' in inspect.signature(self.function).parameters:
            kwargs['model'] = _ocr_model()
        self.function(**kwargs)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, function in list(globals().items()):
        if not name.startswith('test_') or not callable(function):
            continue
        dimensions = getattr(function, 'parameters', [])
        for values in itertools.product(*(dimension[1] for dimension in dimensions)):
            kwargs = {}
            for (names, _), value in zip(dimensions, values):
                kwargs.update(zip(names, value if len(names)>1 else (value,)))
            suite.addTest(OfflineCase(function, kwargs))
    return suite


def source(path, revision=None):
    if revision:
        return subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT).decode('utf-8')
    return (ROOT/path).read_text(encoding='utf-8')


def compile_nodes(nodes, namespace):
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(tree, '<actual-production-source>', 'exec'), namespace)


def image_namespace(revision=None):
    ns = dict(cv2=cv2, np=np, fromfile=np.fromfile, uint8=np.uint8, float32=np.float32,
              int32=np.int32, Path=Path, cached_property=cached_property,
              RuleImageMallResourceMixin=object, logger=Mock(), get_current_humanizer=lambda: None)
    cl = next(n for n in ast.parse(source('module/atom/image.py', revision)).body
              if isinstance(n, ast.ClassDef) and n.name == 'RuleImage')
    compile_nodes([cl], ns)
    return ns


def load_shop(revision=None):
    # Always test the hotfix against either current or stable-main dependencies.
    ns = image_namespace(revision)
    compile_nodes(ast.parse(source('module/exception.py', revision)).body, ns)
    ns.update(re=re, time=SimpleNamespace(monotonic=lambda: 0), cv2=cv2, np=np,
              page_main='main', page_mall='mall',
              MysteryShopAssets=SimpleNamespace(I_ME_ENTER='enter', I_MS_SHARE='share'))
    tree = ast.parse(source('tasks/DailyAltAcc/mshop.py'))
    error = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MshopRefreshError')
    cl = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Mshop')
    cl.bases = [ast.Name(id='object', ctx=ast.Load())]
    compile_nodes([error, cl], ns)
    return ns


def method(path, cls, name, ns, revision=None):
    cl = next(n for n in ast.parse(source(path, revision)).body if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in cl.body if isinstance(n, ast.FunctionDef) and n.name == name)
    compile_nodes([fn], ns)
    return ns[name]


def load_image_rules(ns, revision=None):
    wanted = {'I_MS_REFRESH', 'I_MS_ENSURE', 'I_M_PICTURE_REFUSE'}
    cl = next(n for n in ast.parse(source('tasks/DailyAltAcc/assets.py', revision)).body
              if isinstance(n, ast.ClassDef) and n.name == 'DailyAltAccAssets')
    return {n.targets[0].id: eval(compile(ast.Expression(n.value), '<actual-asset>', 'eval'), ns)
            for n in cl.body if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') in wanted}


def read_rgb(path):
    return cv2.cvtColor(cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), 1), cv2.COLOR_BGR2RGB)


@lru_cache(maxsize=1)
def _ocr_model():
    # Reuse the existing isolated OCR loader; no business modules are imported.
    import runpy
    existing = runpy.run_path(str(ROOT/'dev_tools/test_popup_ui_20261010.py'), run_name='offline_fixture')
    return existing['ocr_model']()[0]


def image_shop(image, model, revision=None):
    ns = load_shop(revision)
    shop = ns['Mshop']()
    for name, rule in load_image_rules(ns, revision).items():
        setattr(shop, name, rule)
    shop.device = SimpleNamespace(image=image)
    shop.O_EXP_DAILOG = SimpleNamespace(model=model)
    shop.appear = lambda rule: rule.match(shop.device.image)
    cl = next(n for n in ast.parse(source('tasks/MysteryShop/assets.py', revision)).body
              if isinstance(n, ast.ClassDef) and n.name == 'MysteryShopAssets')
    node = next(n for n in cl.body if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'I_MS_SHARE')
    ns['MysteryShopAssets'].I_MS_SHARE = eval(compile(ast.Expression(node.value), '<actual-share-asset>', 'eval'), ns)
    def rgb(rule):
        if not rule.match(shop.device.image):
            return False
        x,y,w,h=map(int,rule.roi_front)
        return bool(np.max(np.abs(rule.image.mean((0,1))-shop.device.image[y:y+h,x:x+w].mean((0,1))))<=10)
    shop.appear_rgb = rgb
    return shop


OLD = Path(os.environ['OAS_MSHOP_OLD_FRAME']) if os.environ.get('OAS_MSHOP_OLD_FRAME') else None
NEW = Path(os.environ['OAS_MSHOP_COMPOSITE']) if os.environ.get('OAS_MSHOP_COMPOSITE') else None
LD_DARK = Path(os.environ['OAS_MSHOP_LD_DARK']) if os.environ.get('OAS_MSHOP_LD_DARK') else None
LD_LIMIT = Path(os.environ['OAS_MSHOP_LD_LIMIT']) if os.environ.get('OAS_MSHOP_LD_LIMIT') else None
BRIGHT = Path(os.environ['OAS_MSHOP_BRIGHT']) if os.environ.get('OAS_MSHOP_BRIGHT') else None


@cases('revision', [None, 'main'])
@cases('label,path', [('old_mumu_raw', OLD), ('new_mumu_composite_local_region', NEW)])
def test_real_refresh_dialog(label, path, revision, model):
    if path is None or not path.is_file():
        raise unittest.SkipTest(f'{label}: private image unavailable')
    image = read_rgb(path)
    if label.startswith('new'):
        # Reposition the visible game region, no resize. Missing pixels stay black.
        # This is explicitly a reconstructed local-ROI test, NOT a raw device frame.
        restored = np.zeros((720, 1280, 3), np.uint8)
        restored[120:720, 204:1280] = image[:600, :1076]
        image = restored
    shop = image_shop(image, model, revision)
    evidence = shop._mshop_dialog()
    button = shop._MS_CONFIRM
    _, button_score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(button.corp(image), button.image, cv2.TM_CCOEFF_NORMED))
    guard = shop.I_MS_ENSURE.guard
    _, guard_score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(guard.corp(image), guard.image, cv2.TM_CCOEFF_NORMED))
    REPORT['recognition'].append(dict(label=label, dependency=revision or 'dev',
        button_score=button_score, guard_score=guard_score, **evidence))
    assert evidence['valid'] and evidence['cost'] == 5
    assert evidence['legacy'] == label.startswith('old')


@cases('suffix', ['111359-085', '111403-197', '111421-376'])
def test_real_other_dialog_rejected(suffix, model):
    if OLD is None:
        raise unittest.SkipTest('private negative frame unavailable')
    path = OLD.parent/f'MuMu-20261010-{suffix}.png'
    if not path.exists():
        raise unittest.SkipTest('private negative frame unavailable')
    shop = image_shop(read_rgb(path), model)
    result = shop._mshop_dialog()
    REPORT['recognition'].append(dict(label=f'other_dialog_{suffix}', **result))
    assert not result['valid']


@cases('text,score,expected', [
    ('5(仅1次)', .99, (5, 1)), ('12（仅2次）', .99, (12, 2)),
    ('5(仅0次)', .99, (5, 0)), ('5(仅1次)', .8, None),
    ('5仅1次', .99, None), ('15(仅1次)其他', .99, None),
    ('金币5(仅1次)', .99, None), ('5(1次)', .99, None)])
def test_offer_exact_parsing(text, score, expected):
    ns = load_shop(); shop = ns['Mshop']()
    shop.O_EXP_DAILOG = SimpleNamespace(model=SimpleNamespace(ocr_single_line=lambda im: (text, score)))
    assert shop._mshop_offer(np.zeros((40, 100, 3), np.uint8), (0, 0, 100, 40)) == expected


@cases('text,score,expected', [
    ('x5', .99, 5), ('×12', .99, 12), ('X15', .99, 15),
    ('x5', .8, None), ('5', .99, None), ('x5金币', .99, None), ('x0', .99, None)])
def test_cost_exact_parsing(text, score, expected):
    ns = load_shop(); shop = ns['Mshop']()
    shop.O_EXP_DAILOG = SimpleNamespace(model=SimpleNamespace(ocr_single_line=lambda im: (text, score)))
    assert shop._mshop_cost(np.zeros((50, 50, 3), np.uint8), (0, 0, 50, 50)) == expected


@cases('failure', ['purchase_title', 'partial_title', 'low_confidence', 'other_currency', 'missing_confirm', 'bad_layout'])
def test_similar_dialog_must_not_confirm(failure, model):
    if OLD is None or not OLD.is_file():
        raise unittest.SkipTest('private original frame unavailable')
    image = read_rgb(OLD)
    shop = image_shop(image, model)
    original_text, original_appear = shop._mshop_text, shop.appear
    if failure in ('purchase_title', 'partial_title', 'low_confidence'):
        replacement = {'purchase_title': '购买神秘商店货物需消耗', 'partial_title': '刷新神秘商店', 'low_confidence': ''}[failure]
        shop._mshop_text = lambda im, roi: replacement if roi == shop._MS_TITLE_ROI else original_text(im, roi)
    elif failure == 'other_currency':
        image[289:346, 746:804] = 0
    elif failure == 'missing_confirm':
        shop.appear = lambda rule: False if rule is shop._MS_CONFIRM else original_appear(rule)
    elif failure == 'bad_layout':
        def appear(rule):
            found = original_appear(rule)
            if rule is shop.I_M_PICTURE_REFUSE:
                rule.roi_front[1] += 35
            return found
        shop.appear = appear
    result = shop._mshop_dialog()
    REPORT['recognition'].append(dict(label='synthetic_'+failure, **result))
    assert not result['valid']


def simulator(case, revision=None, backend='mumu'):
    ns = load_shop(revision)
    class Sim(ns['Mshop']):
        I_CHECK_MAIN='main'; I_CHECK_MALL='mall'; I_BACK_MAIN='back_main'; I_BACK_Y='back'
        I_MS_REFRESH='refresh'; C_MS_REFRESH_ACTION='request'; I_MS_ENSURE='ensure'
        I_M_PICTURE_REFUSE='cancel'; _MS_CONFIRM='confirm'
        def __init__(self):
            self.now=0.; self.phase='shelf'; self.tick=0; self.actions=[]; self.scans=0
            self.case=case; self.backend=backend
            self.device=SimpleNamespace(image=np.zeros((720,1280,3),np.uint8))
            self.I_MS_REFRESH=SimpleNamespace(image=np.zeros((31,103,3),np.uint8))
            self.I_MS_ENSURE=SimpleNamespace(guard=SimpleNamespace(image=np.zeros((45,380,3),np.uint8)))
            ns['time'].monotonic=lambda:self.now
        def screenshot(self):
            self.now+=.25; self.tick+=1
            if self.phase=='waiting':
                if self.case=='screenshot_error': raise OSError('fake screenshot failure')
                if self.case in ('unknown','no_dialog'): pass
                elif self.tick >= (7 if self.case=='delayed' else 1): self.phase='dialog'; self.tick=0
            elif self.phase=='dialog' and self.case=='disappears' and self.tick>=1:
                self.phase='cancelled'
            elif self.phase=='result' and self.case=='insufficient':
                self.phase='resource_error'
            self.device.image.fill(50 if self.phase=='result' else 0)
            if self.phase=='result' and self.case=='unchanged': self.device.image.fill(0)
            if self.phase=='result' and self.case=='unstable': self.device.image.fill(40+(self.tick%2)*20)
            return self.device.image
        def ui_get_current_page(self):return 'main'
        def ui_goto(self, *a, **k):return True
        def appear_then_click(self, *a, **k):return False
        def _save_mshop_page(self, index):pass
        def MsFind(self):self.scans+=1;return self.case=='item_found'
        def _mshop_overlay(self):return self.phase in ('dialog','resource_error')
        def _mshop_shelf_visible(self):return self.phase in ('shelf','result','cancelled') or (self.phase=='waiting' and self.case=='no_dialog')
        def _mshop_dialog(self):return dict(valid=self.case not in ('bad_dialog','insufficient'),cost=10 if self.case=='fee_changed' else 5)
        def _mshop_button_state(self):
            if self.case=='no_opportunity':return 'DISABLED'
            return 'ENABLED'
        def _mshop_limit_reached(self):return False
        def _mshop_offer(self, im, roi):
            if im is self.device.image:
                if self.case=='offer_changed':return (10,1)
                if self.phase=='result':return (5,0)
            return (5,1)
        def _mshop_cost(self,*a):return 5
        def appear_rgb(self,rule):
            if rule=='main':return self.phase=='main'
            if rule=='mall':return False
            if rule in ('back_main','back'):return True
            return self.case!='no_opportunity' # Background refresh can ALWAYS match.
        def click(self,rule):
            self.actions.append(rule)
            if rule=='request':self.phase='waiting';self.tick=0
            elif rule=='confirm':
                self.phase='dialog' if self.case=='confirm_stuck' else ('cancelled' if self.case=='user_cancel' else 'result')
            elif rule=='cancel':self.phase='cancelled'
            elif rule in ('back_main','back'):self.phase='main'
    return Sim(), ns


CASES = ['normal','delayed','no_dialog','confirm_stuck','disappears','user_cancel',
         'fee_changed','offer_changed','no_opportunity','insufficient','unstable',
         'unchanged','screenshot_error','unknown','bad_dialog','item_found']


@cases('revision',[None,'main'])
@cases('backend',['mumu','ldplayer'])
@cases('case',CASES)
def test_bounded_transaction(case, backend, revision):
    sim, ns = simulator(case, revision, backend)
    error=None
    try: sim.run_mysteryshop()
    except Exception as exc:error=exc
    requests=sim.actions.count('request');confirms=sim.actions.count('confirm')
    result=sim._mshop_result
    REPORT['flow'].append(dict(case=case, backend=backend, dependency=revision or 'dev',
        actual_requests=requests, actual_confirms=confirms, **result))
    assert requests<=1 and confirms<=1 and sim.now<40
    if case in ('normal','delayed'):
        assert error is None and requests==confirms==result['completed']==1 and sim.scans==2
    elif case in ('item_found','no_opportunity'):
        assert error is None and requests==confirms==result['completed']==0
    else:
        assert error is not None and result['outcome']=='FAILED' and result['completed']==0
    if case in ('fee_changed','offer_changed','no_dialog','unknown','bad_dialog','disappears'):
        assert confirms==0
    if case=='screenshot_error':assert isinstance(error,OSError)


@cases('revision',[None,'main'])
def test_failed_shop_uses_existing_daily_adapter_and_next_account(revision):
    sim,ns=simulator('confirm_stuck',revision)
    ns.update(StatEvent=SimpleNamespace(TASK_START='start',TASK_END='end',ERROR='error'),
              STATUS_FAILED='failed',STATUS_DONE='done',CoopStoreWriteError=type('CoopStoreWriteError',(Exception,),{}))
    run=method('tasks/DailyAltAcc/script_task.py','ScriptTask','_run_with_stat',ns,revision)
    ns['time'].time=lambda:sim.now
    events=[];marks=[]
    adapter=SimpleNamespace(_DEVICE_LEVEL_ERRORS=(ns['GamePageUnknownError'],),_progress=object(),_progress_key='fake',
        emit_stat=lambda ev,**kw:events.append((ev,kw)),_should_resume_instead_of_fail=lambda *a:False,
        _mark_progress=lambda task,status,**kw:marks.append((task,status)) or True,
        _notify_task_failed=lambda *a:None,_mark_progress_false=lambda *a,**kw:None)
    assert run(adapter,'mysteryshop',sim.run_mysteryshop) is None
    assert ('mysteryshop','failed') in marks and any(ev=='end' and kw['ok'] is False for ev,kw in events)
    # Existing wrapper can proceed to an unrelated task and the next account.
    assert run(adapter,'cooperation',lambda:True) is True
    sim.case='normal';sim.phase='shelf';sim.actions=[]
    assert run(adapter,'mysteryshop',sim.run_mysteryshop) is None  # normal method preserves None return
    assert sim._mshop_result['completed']==1 and sim._mshop_result['confirms']==1
    assert events[-1][1]['ok'] is True


def test_unknown_recovery_preserves_scheduler_error():
    sim,ns=simulator('unknown')
    with unittest.TestCase().assertRaises(ns['GamePageUnknownError']):sim.run_mysteryshop()
    assert sim._mshop_result['outcome']=='FAILED' and sim._mshop_result['return_main'] is False


def test_source_has_no_backend_or_double_coordinate_conversion():
    tree=ast.parse(source('tasks/DailyAltAcc/mshop.py'))
    cl=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Mshop')
    methods=[n for n in cl.body if isinstance(n,ast.FunctionDef) and
             (n.name=='run_mysteryshop' or n.name.startswith('_mshop_'))]
    for fn in methods:
        attrs={n.attr for n in ast.walk(fn) if isinstance(n,ast.Attribute)}
        assert not attrs.intersection({'nemu_ipc','click_nemu_ipc','click_window_message','window_scale_rate','SendMessage'})


def test_actual_mumu_screenshot_contract():
    ns={'cv2':cv2}
    capture=method('module/device/method/nemu_ipc.py','NemuIpc','screenshot_nemu_ipc',ns)
    rgb=np.array([[[241,13,37],[11,239,67]],[[19,47,233],[83,127,181]]],np.uint8)
    raw=cv2.flip(cv2.cvtColor(rgb,cv2.COLOR_RGB2RGBA),0)
    out=capture(SimpleNamespace(nemu_ipc=SimpleNamespace(screenshot=lambda:raw)))
    assert np.array_equal(out,rgb)


def test_actual_generic_click_preserves_authoring_coordinates():
    class RuleClick:
        name='shop_request';roi_front=(1202,534,55,52)
        def coord(self):return (1220,560)
    ns={'RuleClick':RuleClick,'RuleImage':type('RuleImage',(),{}),
        'RuleOcr':type('RuleOcr',(),{}),'RuleLongClick':type('RuleLongClick',(),{})}
    click=method('tasks/base_task.py','BaseTask','click',ns)
    for backend in ('mumu','ldplayer'):
        sent=[]
        host=SimpleNamespace(device=SimpleNamespace(click=lambda **kw:sent.append(kw)))
        click(host,RuleClick())
        assert sent==[dict(x=1220,y=560,control_name='shop_request',control_roi=(1202,534,55,52))]


@cases('backend', ['mumu', 'ldplayer'])
def test_actual_windows_backend_dispatch(backend):
    sent=[]
    ns=dict(random=SimpleNamespace(randint=lambda *a:100),time=SimpleNamespace(sleep=lambda t:None),
        SendMessage=lambda *a:sent.append(a),PostMessage=lambda *a:sent.append(a),
        MAKELONG=lambda x,y:(x,y),WM_ACTIVATE=1,WA_ACTIVE=2,WM_LBUTTONDOWN=3,
        WM_LBUTTONUP=4,WM_MOUSEMOVE=5,MK_LBUTTON=6)
    click=method('module/device/method/windows_impl.py','Window','click_window_message',ns)
    handles=[101,102] if backend=='mumu' else [102]
    host=SimpleNamespace(is_desktop_window=False,window_scale_rate=1.5,
                          _humanizer=None,control_handle_list=handles)
    click(host,1220,560)
    touches=[item for item in sent if item[1] in (3,4)]
    assert touches==[(102,3,0,(813,373)),(102,4,0,(813,373))]


def test_actual_ldplayer_window_screenshot_input_contract():
    rgb=np.array([[[241,13,37],[11,239,67]],[[19,47,233],[83,127,181]]],np.uint8)
    raw=cv2.cvtColor(rgb,cv2.COLOR_RGB2BGRA).tobytes()
    gdi=Mock();bitmap=Mock();bitmap.GetBitmapBits.return_value=raw
    ns=dict(cv2=cv2,frombuffer=np.frombuffer,GetWindowDC=lambda h:1,
            CreateDCFromHandle=lambda h:gdi,CreateBitmap=lambda:bitmap,SRCCOPY=1,
            DeleteObject=lambda h:None,ReleaseDC=lambda *a:None)
    capture=method('module/device/method/windows_impl.py','Window','screenshot_window_background',ns)
    out=capture(SimpleNamespace(is_desktop_window=False,screenshot_size=(2,2),screenshot_handle_num=102))
    assert np.array_equal(out,rgb)


@cases('revision', [None, 'main'])
def test_actual_multi_account_adapter_after_shop_failure(revision):
    sim,ns=simulator('confirm_stuck',revision)
    ns.update(StatEvent=SimpleNamespace(TASK_START='start',TASK_END='end',ERROR='error'),
              STATUS_FAILED='failed',STATUS_DONE='done',CoopStoreWriteError=type('CoopStoreWriteError',(Exception,),{}))
    daily=method('tasks/DailyAltAcc/script_task.py','ScriptTask','_run_with_stat',ns,revision)
    multi=method('tasks/MultiDailyAltAcc/script_task.py','ScriptTask','_execute_daily_tasks',ns,revision)
    ns['time'].time=lambda:sim.now
    child=SimpleNamespace(_DEVICE_LEVEL_ERRORS=(ns['GamePageUnknownError'],),_progress=object(),_progress_key='fake',
        emit_stat=Mock(),_should_resume_instead_of_fail=lambda *a:False,
        _mark_progress=lambda *a,**kw:True,_notify_task_failed=Mock(),_mark_progress_false=Mock())
    def run():
        daily(child,'mysteryshop',sim.run_mysteryshop)
        raise ns['TaskEnd']([])
    child.run=run
    parent=SimpleNamespace(_DEVICE_LEVEL_ERRORS=(ns['GamePageUnknownError'],),
        _create_task_instance=lambda *a:child,_handle_task_end=lambda *a:True,
        _emit_account_end=Mock(),_retry_account_identity='old',_emit_account_error=Mock(),
        _save_recovery_error_log=Mock())
    assert multi(parent,object(),object()) is True
    assert child._subtask_error_occurred and parent._retry_account_identity is None
    sim.case='normal';sim.phase='shelf';sim.actions=[]
    assert multi(parent,object(),object()) is True
    assert sim._mshop_result['completed']==1


def test_legacy_offer_is_read_from_existing_resource(model):
    shop=image_shop(np.zeros((720,1280,3),np.uint8),model)
    image=shop.I_MS_REFRESH.image
    offer=shop._mshop_offer(image,(22,0,image.shape[1]-22,image.shape[0]))
    image=shop.I_MS_ENSURE.guard.image
    cost=shop._mshop_cost(image,(347,0,image.shape[1]-347,image.shape[0]))
    assert offer is not None and offer[0]==cost and offer[1]>0


def test_return_main_must_be_verified():
    sim,ns=simulator('normal')
    original=sim.appear_rgb
    sim.appear_rgb=lambda rule:False if rule=='main' else original(rule)
    with unittest.TestCase().assertRaises(ns['GamePageUnknownError']):sim.run_mysteryshop()
    assert sim._mshop_result['outcome']=='FAILED'


def test_zero_flower_still_means_goods_unlock_level():
    ns=load_shop();shop=ns['Mshop']();seen=[]
    shop.get_config=lambda:SimpleNamespace(daily_alt_acc_config=SimpleNamespace(isflower=0))
    shop.screenshot=lambda:None
    shop._scan_slots=lambda flower:seen.append(flower) or []
    assert shop.MsFind() is False and seen==[0]


def test_goods_and_purchase_rules_unchanged_from_stable_main():
    def functions(text):
        cl=next(n for n in ast.parse(text).body if isinstance(n,ast.ClassDef) and n.name=='Mshop')
        result={}
        for n in cl.body:
            if isinstance(n,ast.FunctionDef) and n.name in ('_ocr_price','_scan_slots','MsFind','_should_buy','_notify_item'):
                if isinstance(n.body[0],ast.Expr) and isinstance(n.body[0].value,ast.Constant):
                    n.body=n.body[1:]
                result[n.name]=ast.dump(n,include_attributes=False)
        return result
    assert functions(source('tasks/DailyAltAcc/mshop.py'))==functions(source('tasks/DailyAltAcc/mshop.py','main'))


def test_original_p2_counterexample_now_rejects_unverified_completion():
    sim,ns=simulator('user_cancel')
    capture,rgb,offer=sim.screenshot,sim.appear_rgb,sim._mshop_offer
    def changed_shelf():
        image=capture()
        if sim.phase=='cancelled':image.fill(50)
        return image
    sim.screenshot=changed_shelf
    sim.appear_rgb=lambda rule:False if rule is sim.I_MS_REFRESH and sim.phase=='cancelled' else rgb(rule)
    sim._mshop_offer=lambda image,roi:None if image is sim.device.image and sim.phase=='cancelled' else offer(image,roi)
    with unittest.TestCase().assertRaises(ns['MshopRefreshError']):sim.run_mysteryshop()
    assert sim._mshop_result['completed']==0 and sim._mshop_result['outcome']=='FAILED'
    assert sim.actions.count('request')==sim.actions.count('confirm')==1
    REPORT['flow'].append(dict(case='ORIGINAL_P2',**sim._mshop_result))


def required_image(path):
    if path is None or not path.is_file():
        raise unittest.SkipTest('optional private button frame unavailable')
    return read_rgb(path)


@cases('label,path,expected,limit', [
    ('local_raw_bright',BRIGHT,'ENABLED',False),
    ('ld_raw_disabled',LD_DARK,'DISABLED',False),
    ('ld_raw_limit',LD_LIMIT,'DISABLED',True),
    ('mumu_raw_overlay',OLD,'UNKNOWN',False)])
def test_real_button_states(label,path,expected,limit):
    shop=image_shop(required_image(path),None)
    evidence=shop._mshop_button_evidence()
    REPORT['button'].append(dict(label=label,**evidence))
    assert evidence['state']==expected and shop._mshop_limit_reached() is limit


@cases('mutation', ['missing','between_states','too_bright','border_missing','unknown_page','dynamic_fee'])
def test_button_unknown_and_fee_exclusion(mutation):
    image=required_image(LD_DARK)
    if mutation=='missing':image[525:589,1195:1269]=0
    elif mutation=='between_states':
        bright=required_image(BRIGHT)
        image[525:589,1195:1269]=((image[525:589,1195:1269].astype(np.uint16)+bright[525:589,1195:1269])/2).astype(np.uint8)
    elif mutation=='too_bright':
        image[525:589,1195:1269]=np.minimum(required_image(BRIGHT)[525:589,1195:1269].astype(float)*1.3,255).astype(np.uint8)
    elif mutation=='border_missing':image[523:536,1197:1265]=0
    elif mutation=='unknown_page':image.fill(0)
    elif mutation=='dynamic_fee':image[589:620,1172:1275]=np.random.default_rng(123).integers(0,256,(31,103,3),dtype=np.uint8)
    shop=image_shop(image,None)
    evidence=shop._mshop_button_evidence()
    REPORT['button'].append(dict(label='synthetic_'+mutation,**evidence))
    assert evidence['state']==('DISABLED' if mutation=='dynamic_fee' else 'UNKNOWN')


def test_both_color_candidates_return_unknown():
    shop=image_shop(required_image(BRIGHT),None)
    reference=shop._MS_REFRESH_ENABLED.image[8:64,:70]
    hist=shop._mshop_color_hist(reference)
    shop.__dict__['_mshop_button_references']=[('ENABLED',reference,hist),('DISABLED',reference,hist)]
    assert shop._mshop_button_state()=='UNKNOWN'


def test_all_available_local_shelves_separate_by_color():
    directory=os.environ.get('OAS_MSHOP_SHELF_DIR')
    if not directory:
        raise unittest.SkipTest('optional historical shelf directory unavailable')
    paths=list(Path(directory).glob('*.png'))
    assert paths
    counts={'ENABLED':0,'DISABLED':0,'UNKNOWN':0}
    for path in paths:
        image=read_rgb(path)
        if image.shape!=(720,1280,3):continue
        evidence=image_shop(image,None)._mshop_button_evidence()
        counts[evidence['state']]+=1
        REPORT['button'].append(dict(label='historical_local_shelf',sample_sha256=__import__('hashlib').sha256(path.read_bytes()).hexdigest(),**evidence))
        assert evidence['state']!='UNKNOWN'
    assert counts['ENABLED']>0 and counts['DISABLED']>0
    REPORT['compatibility']['historical_color_clusters']=counts


STATE_CASES=['enabled_to_disabled','enabled_with_reduced_offer','enabled_offer_unchanged',
    'initial_disabled','initial_unknown','result_unknown','result_unknown_offer_reduced',
    'disabled_footer_hidden','initial_limit','result_limit_unknown','free_offer',
    'paid_fee_unknown','paid_wrong_title','paid_confirm_stuck','unstable_result','no_response']


def state_simulator(case,revision=None,backend='mumu'):
    old_case={'paid_fee_unknown':'bad_dialog','paid_wrong_title':'bad_dialog',
              'paid_confirm_stuck':'confirm_stuck','unstable_result':'unstable',
              'no_response':'no_dialog'}.get(case,'normal')
    sim,ns=simulator(old_case,revision,backend)
    original_offer=sim._mshop_offer
    def state():
        if case=='initial_disabled':return 'DISABLED'
        if case=='initial_unknown':return 'UNKNOWN'
        if sim.phase=='result':
            if case in ('result_unknown','result_unknown_offer_reduced','result_limit_unknown'):return 'UNKNOWN'
            if case in ('enabled_offer_unchanged','enabled_with_reduced_offer'):return 'ENABLED'
            return 'DISABLED'
        return 'ENABLED'
    sim._mshop_button_state=state
    def offer(image,roi):
        if image is sim.device.image:
            if case=='free_offer':return (0,1)
            if sim.phase=='result' and case in ('disabled_footer_hidden','result_unknown'):return None
            if sim.phase=='result' and case=='enabled_offer_unchanged':return (5,1)
        return original_offer(image,roi)
    sim._mshop_offer=offer
    if case=='paid_fee_unknown':
        sim._mshop_dialog=lambda:dict(valid=False,cost=None)
    sim._mshop_limit_reached=lambda:case=='initial_limit' or (case=='result_limit_unknown' and sim.phase=='result')
    return sim,ns


@cases('revision',[None,'main'])
@cases('backend',['mumu','ldplayer'])
@cases('case',STATE_CASES)
def test_three_state_transaction(case,backend,revision):
    sim,ns=state_simulator(case,revision,backend)
    error=None
    try:sim.run_mysteryshop()
    except Exception as exc:error=exc
    result=sim._mshop_result
    requests,confirms=sim.actions.count('request'),sim.actions.count('confirm')
    REPORT['flow'].append(dict(case='THREE_STATE_'+case,backend=backend,dependency=revision or 'dev',
        actual_requests=requests,actual_confirms=confirms,**result))
    assert requests<=1 and confirms<=1 and sim.now<40
    if case in ('enabled_to_disabled','enabled_with_reduced_offer','disabled_footer_hidden'):
        assert error is None and requests==confirms==result['completed']==1
        assert result['refresh_outcome']=='SUCCESS'
    elif case in ('initial_disabled','initial_limit'):
        assert error is None and requests==confirms==result['completed']==0
        assert result['refresh_outcome'] in ('ALREADY_REFRESHED','LIMIT_REACHED')
        assert sim.scans==1
    else:
        assert error is not None and result['completed']==0 and result['outcome']=='FAILED'
    if case in ('initial_disabled','initial_unknown','initial_limit','free_offer'):
        assert requests==confirms==0
    if case in ('paid_fee_unknown','paid_wrong_title'):
        assert requests==1 and confirms==0


@cases('revision',[None,'main'])
def test_disabled_still_runs_actual_goods_scan_and_notification(revision):
    sim,ns=state_simulator('initial_disabled',revision)
    item=SimpleNamespace(goods=SimpleNamespace(name='configured_good'),
                         coin=SimpleNamespace(name='jade'),price=55,slot=1)
    found=[]
    sim.get_config=lambda:SimpleNamespace(daily_alt_acc_config=SimpleNamespace(isflower=0))
    sim._scan_slots=lambda flower:[item]
    sim._should_buy=lambda *args:True
    sim._notify_item=lambda record:found.append(record)
    sim.MsFind=lambda:ns['Mshop'].MsFind(sim)
    sim.run_mysteryshop()
    assert found==[item] and sim._mshop_result['outcome']=='ALREADY_REFRESHED'
    assert sim._mshop_result['completed']==0 and not sim.actions.count('request') and not sim.actions.count('confirm')


@cases('missing',['request','confirmation','dialog','initial_enabled'])
def test_dark_state_alone_never_proves_this_execution_completed(missing):
    sim,ns=simulator('normal')
    sim._mshop_result=dict(initial_button='ENABLED',requests=1,confirms=1,dialog_verified=True)
    if missing=='request':sim._mshop_result['requests']=0
    if missing=='confirmation':sim._mshop_result['confirms']=0
    if missing=='dialog':sim._mshop_result['dialog_verified']=False
    if missing=='initial_enabled':sim._mshop_result['initial_button']='DISABLED'
    assert not sim._mshop_completion_evidence('DISABLED',(5,1),None)


def test_button_reuses_one_frame_without_ocr():
    from unittest.mock import patch
    model=SimpleNamespace(ocr_single_line=Mock(side_effect=AssertionError('button must not OCR')))
    shop=image_shop(required_image(LD_DARK),model)
    shop._mshop_frame_cache={}
    original=cv2.matchTemplate
    with patch.object(cv2,'matchTemplate',wraps=original) as matcher:
        first=shop._mshop_button_evidence();count=matcher.call_count
        assert shop._mshop_button_evidence() is first
        assert shop._mshop_button_state()=='DISABLED' and matcher.call_count==count
    assert model.ocr_single_line.call_count==0


def test_frame_cache_resets_even_if_device_reuses_same_array():
    shop=image_shop(required_image(LD_DARK),None)
    shop._mshop_frame_cache={}
    assert shop._mshop_button_state()=='DISABLED'
    bright=required_image(BRIGHT)
    shop.screenshot=lambda:shop.device.image.__setitem__((slice(525,589),slice(1195,1269)),bright[525:589,1195:1269])
    shop._mshop_capture()
    assert shop._mshop_button_state()=='ENABLED'


def test_exact_local_ocr_crop_cache_is_bounded_and_not_stale():
    ns=load_shop();shop=ns['Mshop']()
    engine=Mock(return_value=('x5',.99));shop.O_EXP_DAILOG=SimpleNamespace(model=SimpleNamespace(ocr_single_line=engine))
    image=np.zeros((50,50,3),np.uint8)
    assert shop._mshop_cost(image,(0,0,50,50))==5
    assert shop._mshop_cost(image.copy(),(0,0,50,50))==5 and engine.call_count==1
    for i in range(1,25):
        image[0,0,0]=i;shop._mshop_cost(image,(0,0,50,50))
    assert engine.call_count==25 and len(shop._mshop_ocr_cache)<=16


def test_real_enabled_offer_matches_existing_paid_policy(model):
    shop=image_shop(required_image(BRIGHT),model)
    assert shop._mshop_button_state()=='ENABLED'
    resource=shop.I_MS_REFRESH.image
    allowed=shop._mshop_offer(resource,(22,0,resource.shape[1]-22,resource.shape[0]))
    assert allowed is not None
    assert shop._mshop_offer(shop.device.image,shop._MS_OFFER_ROI)==allowed


def test_actual_dialog_missing_cost_rejects_even_if_legacy_guard_matches(model):
    shop=image_shop(required_image(OLD),model)
    original=shop._mshop_text
    shop._mshop_text=lambda image,roi:'' if roi==shop._MS_COST_ROI else original(image,roi)
    evidence=shop._mshop_dialog()
    assert evidence['legacy'] is True and evidence['cost'] is None and evidence['valid'] is False


def test_unknown_failure_and_next_account_keep_real_progress_json_valid():
    import tempfile
    nodes=[n for n in ast.parse(source('tasks/Component/MultiAccountRunner/progress.py')).body
           if not (isinstance(n,ast.ImportFrom) and n.module=='module.logger')]
    progress_ns={'logger':Mock()};compile_nodes(nodes,progress_ns)
    sim,ns=state_simulator('initial_unknown')
    ns.update(StatEvent=SimpleNamespace(TASK_START='start',TASK_END='end',ERROR='error'),
              STATUS_FAILED='failed',STATUS_DONE='done',CoopStoreWriteError=type('CoopStoreWriteError',(Exception,),{}))
    run=method('tasks/DailyAltAcc/script_task.py','ScriptTask','_run_with_stat',ns)
    ns['time'].time=lambda:sim.now
    with tempfile.TemporaryDirectory(prefix='oas-mshop-progress-') as directory:
        base=Path(directory).resolve()
        assert base.name.startswith('oas-mshop-progress-')
        store=progress_ns['ProgressStore']('multi_daily','offline_test',base_dir=base)
        store.ensure_phase({'mysteryshop':True},'offline-phase')
        adapter=SimpleNamespace(_DEVICE_LEVEL_ERRORS=(ns['GamePageUnknownError'],),_progress=store,_progress_key='fake-first',
            emit_stat=Mock(),_should_resume_instead_of_fail=lambda *a:False,
            _notify_task_failed=Mock(),_mark_progress_false=Mock())
        adapter._mark_progress=lambda task,status,**kw:store.mark_task(adapter._progress_key,task,status,**kw)
        assert run(adapter,'mysteryshop',sim.run_mysteryshop) is None
        assert store.get_task('fake-first','mysteryshop')['status']=='failed'
        next_sim,_=state_simulator('enabled_to_disabled')
        adapter._progress_key='fake-next'
        assert run(adapter,'mysteryshop',next_sim.run_mysteryshop) is None
        assert store.get_task('fake-next','mysteryshop')['status']=='done'
        files=list(base.glob('*.json'))
        assert files
        for path in files:
            assert path.resolve().is_relative_to(base)
            assert isinstance(json.loads(path.read_text(encoding='utf-8')),dict)


def test_report_export():
    path=os.environ.get('OAS_MSHOP_TEST_REPORT')
    if path:
        Path(path).write_text(json.dumps(REPORT,ensure_ascii=False,indent=2),encoding='utf-8')
