# This Python file uses the following encoding: utf-8
"""PP-OCRv6 端到端验证（一次性工具，不属于测试套件）。

验证三件事：
1. 本地直调 OCR 能出正确结果，并报告实际使用的设备。
2. RPC 服务能起来、能被连上、识别结果与本地一致。
3. 上层 Rule（BaseCor 子类）走完整链路可用。

用法：./toolkit/python.exe -m dev_tools.verify_ocr_v6
"""
import json
import os
import sys
import time

import numpy as np


def make_image(text='54/40', width=420, height=110):
    import cv2

    image = np.full((height, width, 3), 255, dtype=np.uint8)
    cv2.putText(image, text, (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 2,
                (0, 0, 0), 4, cv2.LINE_AA)
    return image


def verify_local(report):
    """本地直调。"""
    from module.ocr.models import get_local_ocr_model

    model = get_local_ocr_model('ch')
    image = make_image('54/40')

    t0 = time.perf_counter()
    single = model.ocr_single_line(image)
    single_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    full = model.detect_and_ocr(make_image('OCR TEST'))
    full_ms = (time.perf_counter() - t0) * 1000

    report['local'] = {
        'device': model.resolved_device,
        'model_type': model.resolved_model_type,
        'single_text': single[0],
        'single_score': round(single[1], 4),
        'single_ms': round(single_ms, 1),
        'full_texts': [r.ocr_text for r in full],
        'full_ms': round(full_ms, 1),
        'ok': single[0] == '54/40' and bool(full),
    }
    return report['local']['ok']


def verify_rule(report):
    """上层 Rule 链路：BaseCor -> get_ocr_model -> 适配器。"""
    from module.ocr.base_ocr import BaseCor

    cor = BaseCor(name='verify', mode='Full', method='Default',
                  roi=(0, 0, 420, 110), area=(0, 0, 420, 110), keyword='')
    text = cor.ocr_single_line(make_image('54/40'))
    boxed = cor.detect_and_ocr(make_image('OCR TEST'))
    report['rule'] = {
        'single_text': text,
        'full_texts': [r.ocr_text for r in boxed],
        'ok': text == '54/40' and bool(boxed),
    }
    return report['rule']['ok']


def verify_rpc(report):
    """RPC 服务：起服务、连上、识别、关闭。"""
    from module.ocr.rpc import (ModelProxy, ensure_ocr_server_started,
                                shutdown_ocr_server)
    from module.server.setting import State

    config = State.deploy_config
    # 临时打开服务开关，仅影响本进程内存中的配置对象
    config.StartOcrServer = True
    port = int(config.OcrServerPort)
    address = f'127.0.0.1:{port}'

    started = ensure_ocr_server_started()
    report['rpc'] = {'started': started, 'address': address}
    if not started:
        report['rpc']['ok'] = False
        return False

    try:
        proxy = ModelProxy(address)
        image = make_image('54/40')

        t0 = time.perf_counter()
        single = proxy.ocr_single_line(image)
        single_ms = (time.perf_counter() - t0) * 1000

        full = proxy.detect_and_ocr(make_image('OCR TEST'))
        vertical = proxy.detect_and_ocr(make_image('12345'), drop_score=0.1,
                                        box_thresh=0.2, vertical=True)

        report['rpc'].update({
            'single_text': single[0],
            'single_score': round(float(single[1]), 4),
            'single_ms': round(single_ms, 1),
            'full_texts': [r.ocr_text for r in full],
            'full_box_type': type(full[0].box).__name__ if full else None,
            'vertical_count': len(vertical),
            'ok': single[0] == '54/40' and bool(full),
        })
        return report['rpc']['ok']
    except Exception as e:
        report['rpc']['error'] = f'{type(e).__name__}: {e}'
        report['rpc']['ok'] = False
        return False
    finally:
        shutdown_ocr_server()


def main() -> int:
    report = {}
    ok = True
    for name, fn in (('local', verify_local), ('rule', verify_rule), ('rpc', verify_rpc)):
        try:
            ok = fn(report) and ok
        except Exception as e:
            import traceback
            report[name] = {'ok': False,
                            'error': f'{type(e).__name__}: {e}',
                            'traceback': traceback.format_exc()[-1500:]}
            ok = False

    report['all_ok'] = ok
    print('@@OCR_V6_VERIFY@@' + json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
