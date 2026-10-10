"""Offline supplied-popup regression. Never imports Device or reads account config.
Run toolkit/python.exe dev_tools/test_popup_ui_20261010.py --output <json>.
"""
import argparse
import ast
from functools import cached_property
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = Path(r'D:\Documents\MuMu共享文件夹\Screenshots')
SHOTS = [('A', SHOT_DIR/'MuMu-20261010-111335-040.png'),
         ('B', SHOT_DIR/'MuMu-20261010-111342-717.png'),
         ('C', SHOT_DIR/'MuMu-20261010-111359-085.png'),
         ('D_off', SHOT_DIR/'MuMu-20261010-111403-197.png'),
         ('D_on', SHOT_DIR/'MuMu-20261010-111406-133.png'),
         ('E', SHOT_DIR/'MuMu-20261010-111421-376.png'),
         ('F', SHOT_DIR/'MuMu-20261010-111612-409.png'),
         ('C_pc', ROOT/'screenshots/desktop_capture/desktop_client_20261010_111749_364249.png'),
         ('D_pc', ROOT/'screenshots/desktop_capture/desktop_client_20261010_112003_194511.png'),
         ('E_pc', ROOT/'screenshots/desktop_capture/desktop_client_20261010_111831_853928.png')]
ASSETS = ['tasks/DailyAltAcc/assets.py', 'tasks/Component/GeneralBattle/assets.py',
          'tasks/Component/GeneralRoom/assets.py', 'tasks/Component/GeneralInvite/assets.py',
          'tasks/RealmRaid/assets.py', 'tasks/GlobalGame/assets.py', 'tasks/GameUi/assets.py',
          'tasks/Restart/assets.py']
TARGETS = ['I_M_PAGE_GET', 'I_M_ENSURE_GET', 'I_M_AWARD', 'I_M_BATCH_AWARD',
           'I_M_BACK_RED', 'I_M_PAGE_MAIL', 'I_M_MAIN_TO_MAIL', 'I_HARVEST_MAIL_ALL',
           'I_EXIT', 'I_EXIT_ENSURE', 'I_FALSE', 'I_REWARD', 'I_CONFIRM', 'I_GI_SURE',
           'I_GI_EXIT_ENSURE', 'I_BACK_YELLOW_SEA', 'I_CHECK_TEAM', 'I_GR_BACK_YELLOW', 'I_MS_ENSURE',
           'I_ENSURE_EXIT', 'I_EXIT3', 'I_REALM_RETRY_NO_REMIND_OFF', 'I_REALM_RETRY_NO_REMIND_ON']

def nodes(path):
    return ast.parse((ROOT/path).read_text(encoding='utf-8')).body

def load_method(path, cls, name, ns):
    cl = next(n for n in nodes(path) if isinstance(n, ast.ClassDef) and n.name == cls)
    n = next(n for n in cl.body if isinstance(n, ast.FunctionDef) and n.name == name)
    n.returns = None
    for arg in n.args.args + n.args.kwonlyargs:
        arg.annotation = None
    exec(compile(ast.Module(body=[n], type_ignores=[]), str(ROOT/path), 'exec'), ns)
    return ns[name]

def image_namespace():
    ns = dict(cv2=cv2, np=np, fromfile=np.fromfile, uint8=np.uint8, float32=np.float32,
              int32=np.int32, Path=Path, cached_property=cached_property,
              RuleImageMallResourceMixin=object, logger=Mock(), get_current_humanizer=lambda: None)
    cl = next(n for n in nodes('module/atom/image.py') if isinstance(n, ast.ClassDef) and n.name == 'RuleImage')
    exec(compile(ast.Module(body=[cl], type_ignores=[]), 'actual_RuleImage', 'exec'), ns)
    return ns

def load_rules():
    ns = image_namespace()
    rules = {}
    for path in ASSETS:
        for cl in nodes(path):
            if not isinstance(cl, ast.ClassDef): continue
            for n in cl.body:
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                    name = n.targets[0].id
                    if name in TARGETS and getattr(n.value.func, 'id', '') == 'RuleImage':
                        rules[name] = eval(compile(ast.Expression(n.value), path, 'eval'), ns)
    return rules, ns

def measurement(rule, image):
    matched = bool(rule.match(image))
    _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(rule.corp(image), rule.image, cv2.TM_CCOEFF_NORMED))
    result = dict(matched=matched, score=score, threshold=rule.threshold,
                  roi=[int(loc[0]+rule.roi_back[0]), int(loc[1]+rule.roi_back[1]),
                       rule.image.shape[1], rule.image.shape[0]], file=rule.file, search=rule.roi_back)
    if getattr(rule, 'guard', None):
        result['guard'] = measurement(rule.guard, image)
    return result

def ocr_model():
    ns = dict(logger=Mock(), cv2=cv2, np=np)
    for path in ('module/ocr/result.py', 'module/ocr/rapid_ocr.py'):
        body = [n for n in nodes(path) if not isinstance(n, ast.ImportFrom)
                or n.module not in ('module.logger', 'module.ocr.result')]
        exec(compile(ast.Module(body=body, type_ignores=[]), path, 'exec'), ns)
    n = next(n for n in nodes('module/ocr/base_ocr.py') if isinstance(n, ast.FunctionDef) and n.name == 'enlarge_canvas')
    exec(compile(ast.Module(body=[n], type_ignores=[]), 'actual_padding', 'exec'), ns)
    ns['re'] = re
    method = load_method('tasks/RealmRaid/script_task.py', 'ScriptTask', '_realm_retry_text', ns)
    return ns['RapidOcrModel'](model_dir=str(ROOT/'toolkit/ocr_models'), device='cpu', model_type='small', cpu_threads=4), ns, method

def audit(with_ocr=True):
    rules, ns = load_rules()
    model, ocr_ns, method = ocr_model() if with_ocr else (None, None, None)
    data = []
    for label, path in SHOTS:
        with Image.open(path) as im:
            image = np.array(im.convert('RGB'))
            entry = dict(label=label, path=str(path), size=im.size, mode=im.mode,
                         sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        assert image.shape == (720,1280,3)
        entry['matches'] = {name: measurement(rule, image) for name, rule in rules.items()}
        if with_ocr:
            entry['ocr'] = {}
            for name, roi in [('title', (548,265,190,37)), ('confirm', (708,403,102,60)),
                              ('retry_button', (805,540,125,45))]:
                def crop(im, roi):
                    x,y,w,h=roi
                    return im[y:y+h,x:x+w]
                rule = SimpleNamespace(roi=roi, model=model, crop=crop, pre_process=lambda im: im,
                                       after_process=lambda text: text)
                sim = SimpleNamespace(device=SimpleNamespace(image=image))
                hits = method(sim, rule)
                raw = model.detect_and_ocr(ocr_ns['enlarge_canvas'](crop(image,roi)))
                entry['ocr'][name] = dict(accepted=hits, raw=[dict(text=r.ocr_text, score=r.score, box=r.box) for r in raw])
        data.append(entry)
    return data

def test_supplied_popup_matrix():
    data = audit()
    for row in data:
        label, matches = row['label'], row['matches']
        for target, expected in [('I_M_ENSURE_GET',label=='A'), ('I_M_BATCH_AWARD', label=='B'),
                                 ('I_EXIT_ENSURE',label.startswith('C')),
                                 ('I_GI_EXIT_ENSURE',label.startswith('E')), ('I_MS_ENSURE',label=='F')]:
            assert matches[target]['matched'] == expected, (label,target,matches[target])
            if expected and target != 'I_M_BATCH_AWARD':
                x,y,w,h=matches[target]['roi']
                button=(673,544,178,62) if label=='A' else (665,408,178,64)
                bx,by,bw,bh=button
                assert bx<=x and by<=y and x+w<=bx+bw and y+h<=by+bh
        if label.startswith('D'):
            assert row['ocr']['title']['accepted'].get('是否再次挑战')
            assert row['ocr']['confirm']['accepted'].get('确定')
            expected='ON' if label=='D_on' else 'OFF'
            assert matches['I_REALM_RETRY_NO_REMIND_'+expected]['matched']
            other='OFF' if expected=='ON' else 'ON'
            assert not matches['I_REALM_RETRY_NO_REMIND_'+other]['matched']
        else:
            assert not row['ocr']['title']['accepted'].get('是否再次挑战')


def pixel_dispatch(image):
    rules, ns = load_rules()
    cl = next(n for n in nodes('module/atom/click.py') if isinstance(n, ast.ClassDef))
    exec(compile(ast.Module(body=[cl], type_ignores=[]), 'actual_RuleClick', 'exec'), ns)
    ns.update(RuleOcr=type('UnusedOcr', (), {}), RuleLongClick=type('UnusedLongClick', (), {}))
    class NoWaitTimer:
        def __init__(self, limit): self.limit = limit
        def reached(self): return True
        def reset(self): pass
    ns['Timer'] = NoWaitTimer
    sim = SimpleNamespace(device=SimpleNamespace(image=image), interval_timer={}, clicks=[])
    sim.device.click = lambda x, y, **kwargs: sim.clicks.append((int(x),int(y),kwargs['control_name']))
    for name in ('appear', 'appear_then_click'):
        fn=load_method('tasks/base_task.py', 'BaseTask', name, ns)
        setattr(sim,name,fn.__get__(sim))
    for name, rule in rules.items(): setattr(sim,name,rule)
    return sim, rules, ns


def test_real_base_task_dispatch_clicks_only_specific_dialogs():
    for label,path in SHOTS:
        image=np.array(Image.open(path).convert('RGB'))
        sim,rules,_=pixel_dispatch(image)
        for name,expected in [('I_M_ENSURE_GET',label=='A'), ('I_EXIT_ENSURE',label.startswith('C')),
                              ('I_GI_EXIT_ENSURE',label.startswith('E')), ('I_MS_ENSURE',label=='F')]:
            sim.clicks.clear()
            assert bool(sim.appear_then_click(rules[name]))==expected
            assert len(sim.clicks)==int(expected)
            if expected:
                # Check the entire sampling ROI, plus samples from the real coord().
                bounds=(673,544,178,62) if label=='A' else (665,408,178,64)
                bx,by,bw,bh=bounds
                for _ in range(50):
                    x,y=rules[name].coord()
                    assert bx<=x<bx+bw and by<=y<by+bh
                    assert not (537<=x<575 and 337<=y<383)
            # A caller's button threshold override must not bypass the title guard.
            if not expected:
                assert not sim.appear_then_click(rules[name],threshold=-1)
                assert not sim.clicks


def test_team_exit_methods_use_actual_supplied_pixels():
    for label,path in SHOTS:
        if not label.startswith('E'): continue
        image=np.array(Image.open(path).convert('RGB'))
        for module,cls,method in [
            ('tasks/Component/GeneralRoom/general_room.py','GeneralRoom','exit_team'),
            ('tasks/BondlingFairyland/script_task.py','ScriptTask','exit_team'),
            ('tasks/Component/GeneralInvite/general_invite.py','GeneralInvite','exit_room'),
            ('tasks/BondlingFairyland/general_invite.py','GeneralInvite','exit_room')]:
            sim,rules,ns=pixel_dispatch(image.copy())
            ns['GeneralInviteAssets']=SimpleNamespace(I_GI_EXIT_ENSURE=rules['I_GI_EXIT_ENSURE'])
            sim.is_in_room=lambda: False  # Background controls hidden by the popup.
            sim.screenshot=lambda: None
            old_click=sim.device.click
            def click(x,y,**kwargs):
                old_click(x,y,**kwargs)
                sim.device.image=np.zeros_like(image)  # Simulated post-confirmation transition.
            sim.device.click=click
            sim.I_BACK_YELLOW=rules['I_GR_BACK_YELLOW']
            fn=load_method(module,cls,method,ns)
            assert fn(sim) is True
            assert len(sim.clicks)==1 and sim.clicks[0][2]=='EXIT_ENSURE'


def test_mail_batch_reward_precedes_background_back_button():
    images={label:np.array(Image.open(path).convert('RGB')) for label,path in SHOTS if label in ('A','B')}
    sim,rules,ns=pixel_dispatch(images['A'])
    ns['GameUiAssets']=SimpleNamespace(I_CHECK_MAIN=object())
    ns['RestartAssets']=SimpleNamespace(I_HARVEST_MAIL_ALL=rules['I_HARVEST_MAIL_ALL'])
    clock=SimpleNamespace(now=0)
    def now():
        clock.now+=1
        return clock.now
    ns['time']=SimpleNamespace(time=now,sleep=lambda value:None)
    def screenshot():
        sim.frames=getattr(sim,'frames',0)+1
        assert sim.frames<25, (sim.phase,sim.clicks)
    sim.screenshot=screenshot
    sim.appear_rgb=lambda target,**kw:sim.appear(target,**kw)
    sim.get_award_daliy=lambda:False
    old_appear=sim.appear
    sim.appear=lambda target,**kw: (True if target is ns['GameUiAssets'].I_CHECK_MAIN and sim.phase=='main'
                                  else False if target is ns['GameUiAssets'].I_CHECK_MAIN
                                  else old_appear(target,**kw))
    sim.phase='confirm'
    old_click=sim.device.click
    def click(x,y,**kwargs):
        old_click(x,y,**kwargs)
        name=kwargs['control_name']
        if name=='MAIL_M_ENSURE_GET':
            assert sim.phase=='confirm';sim.phase='reward';sim.device.image=images['B']
        elif name=='MAIL_M_BATCH_AWARD':
            assert sim.phase=='reward' and 940<=x<1060 and 560<=y<600
            sim.phase='mail';sim.device.image=np.zeros_like(images['B'])
        elif name=='MAIL_M_BACK_RED':
            assert sim.phase=='mail';sim.phase='main'
        else: raise AssertionError(name)
    sim.device.click=click
    # The provided set has no clean mail frame: inject only its back-button presence.
    sim.appear=lambda target,**kw: (True if target is rules['I_M_BACK_RED'] and sim.phase=='mail'
                                  else old_appear_main(target,**kw))
    # Keep the pre-existing main marker simulation separate from genuine A/B matching.
    old_appear_main=lambda target,**kw: (sim.phase=='main' if target is ns['GameUiAssets'].I_CHECK_MAIN
                                        else old_appear(target,**kw))
    fn=load_method('tasks/DailyAltAcc/mail.py','Mail','harvest_mail',ns)
    assert fn(sim) is True
    assert [c[2] for c in sim.clicks]==['MAIL_M_ENSURE_GET','MAIL_M_BATCH_AWARD','MAIL_M_BACK_RED']


def test_guard_generation_matches_asset_definitions():
    ns=dict(Path=Path,name_transform=lambda value:value.upper())
    cl=next(n for n in nodes('dev_tools/assets_extract.py') if isinstance(n,ast.ClassDef) and n.name=='ImageExtractor')
    exec(compile(ast.Module(body=[cl],type_ignores=[]),'actual_ImageExtractor','exec'),ns)
    for resource,asset in [
        ('tasks/Component/GeneralBattle/gb/image.json','tasks/Component/GeneralBattle/assets.py'),
        ('tasks/Component/GeneralInvite/gi/image.json','tasks/Component/GeneralInvite/assets.py'),
        ('tasks/DailyAltAcc/mail/image.json','tasks/DailyAltAcc/assets.py'),
        ('tasks/DailyAltAcc/mshop/image1.json','tasks/DailyAltAcc/assets.py')]:
        for item in json.loads((ROOT/resource).read_text(encoding='utf-8')):
            if not item.get('guard'):continue
            generated=ns['ImageExtractor'](resource,[item]).result
            assignment=next(line.strip() for line in generated.splitlines() if '= RuleImage(' in line)
            assert assignment in (ROOT/asset).read_text(encoding='utf-8')


def test_nine_four_rule_with_genuine_emulator_confirmation_pixels():
    # Reuse the existing state machine fixture; replace exit and retry dialog
    # boundaries with real pixels, actual match(), and the real CPU OCR path.
    # Clean defeat/grid/prepare/fifth-battle frames are simulated, not supplied.
    import importlib.util
    spec=importlib.util.spec_from_file_location('popup_retry_fixture', ROOT/'tests/unit/logic/test_realmraid_retry.py')
    fixture=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    rules,_=load_rules()
    model,ns,ocr=ocr_model()
    pixels={label:np.array(Image.open(path).convert('RGB')) for label,path in SHOTS
            if label in ('C','D_off','D_on')}
    for checkbox in ('off','on'):
        sim=fixture.Sim(popups=(1,2,3,4))
        sim.checkbox=checkbox
        original_screenshot,original_appear,original_click=sim.screenshot,sim.appear,sim.click
        original_text=sim._realm_retry_text
        def screenshot():
            original_screenshot()
            if sim.state=='exit_confirmation':sim.device.image=pixels['C']
            elif sim.state=='popup':sim.device.image=pixels['D_'+sim.checkbox]
            else:sim.device.image=SimpleNamespace(shape=(720,1280,3))
        sim.screenshot=screenshot
        def appear(target,**kwargs):
            if target=='I_EXIT_ENSURE':
                return rules[target].match(sim.device.image) if sim.state in ('exit_confirmation','popup') else False
            if target in ('I_REALM_RETRY_NO_REMIND_OFF','I_REALM_RETRY_NO_REMIND_ON'):
                return rules[str(target)].match(sim.device.image) if sim.state=='popup' else False
            return original_appear(target,**kwargs)
        sim.appear=appear
        def text(rule):
            if sim.state!='popup':return original_text(rule)
            def crop(im,roi):
                x,y,w,h=roi;return im[y:y+h,x:x+w]
            real=SimpleNamespace(roi=rule.roi,model=model,crop=crop,
                                 pre_process=lambda im:im,after_process=lambda text:text)
            return ocr(sim,real)
        sim._realm_retry_text=text
        assert sim.run() is True
        assert len(sim.clicks('I_EXIT_ENSURE'))==4
        assert len(sim.clicks('realm_retry_confirm'))==4
        assert len(sim.clicks('realm_retry_no_remind'))==int(checkbox=='off')
        assert len(sim.clicks('normal_battle'))==1 and sim.current_count==1

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--no-ocr', action='store_true')
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result=audit(not args.no_ocr)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    for row in result:
        print(row['label'], {k:round(v['score'],4) for k,v in row['matches'].items() if v['matched']})
