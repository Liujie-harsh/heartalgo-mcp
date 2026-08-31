# -*- coding: utf-8 -*-
'''heart-algo MCP 12 tools end-to-end smoke: raw MCP over streamable-http.

Usage (heart conda env):
  set MCP_SHARED_SECRET=<same as server>   (optional; fake mode may omit)
  python e2e_mcp_smoke.py
'''
import asyncio
import json
import os
import sys
import tempfile
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE_URL = os.environ.get('HEART_ALGO_MCP_URL', 'http://127.0.0.1:8001/mcp/')
SECRET = os.environ.get('MCP_SHARED_SECRET', '')

EXPECTED_TOOLS = {
    'analyze_case_files', 'compare_diagnoses', 'diagnose_heart_failure',
    'generate_report', 'get_case_detail', 'get_diagnosis_result',
    'get_review_status', 'interpret_diagnosis', 'list_cases',
    'list_supported_views', 'list_tasks', 'submit_review',
}


def make_dicom(path):
    with open(path, 'wb') as f:
        f.write(bytes(128) + b'DICM')


def make_ecg(path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('<ECG><Lead>I</Lead></ECG>')


def dump(title, result):
    print()
    print('=== ' + title + ' ===')
    if getattr(result, 'is_error', False):
        print('[isError] ' + str(result.content)[:600])
        return None
    sc = getattr(result, 'structured_content', None)
    if sc is not None:
        print(json.dumps(sc, ensure_ascii=False, default=str)[:1600])
        return sc
    print(str(result.content)[:400])
    return None


def require(sc, key):
    if sc is None or key not in sc:
        raise SystemExit('[FAIL] missing key in response: ' + key)
    return sc[key]


async def main():
    headers = {}
    if SECRET:
        headers['Authorization'] = 'Bearer ' + SECRET
    http = httpx2.AsyncClient(headers=headers)
    async with streamable_http_client(BASE_URL, http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            print('tools(%d): %s' % (len(names), ', '.join(names)))
            missing = EXPECTED_TOOLS - set(names)
            if missing:
                raise SystemExit('[FAIL] missing tools: ' + ', '.join(sorted(missing)))

            dump('1. list_supported_views', await session.call_tool('list_supported_views', {}))

            tmp = os.path.join(os.getcwd(), '.e2e-tmp')
            os.makedirs(tmp, exist_ok=True)
            dicom_path = os.path.join(tmp, 'echo_plax.dcm')
            ecg_path = os.path.join(tmp, 'ecg_rest.xml')
            make_dicom(dicom_path)
            make_ecg(ecg_path)
            print()
            print('temp files: ' + dicom_path + ' , ' + ecg_path)

            sc = dump('2. analyze_case_files', await session.call_tool('analyze_case_files', {
                'files': [
                    {'path': dicom_path, 'modality': 'CARDIAC_ULTRASOUND', 'dcm_type': 'PLAX'},
                    {'path': ecg_path, 'modality': 'ECG'},
                ],
                'request_id': 'e2e-' + str(int(time.time())),
                'submit': True,
            }))
            case_id = require(sc, 'case_id')
            task_a = require(sc, 'task_id')

            sc = dump('3. get_diagnosis_result(A)', await session.call_tool('get_diagnosis_result', {'task_id': task_a}))
            status_a = require(sc, 'status')

            dump('4. interpret_diagnosis', await session.call_tool('interpret_diagnosis', {'task_id': task_a}))

            dump('5. generate_report(save_to_case)', await session.call_tool('generate_report', {
                'task_id': task_a, 'format': 'markdown', 'save_to_case': True,
            }))

            dump('6. list_cases', await session.call_tool('list_cases', {}))
            dump('7. get_case_detail', await session.call_tool('get_case_detail', {'case_id': case_id}))
            dump('8. list_tasks', await session.call_tool('list_tasks', {'case_id': case_id}))
            dump('9. get_review_status', await session.call_tool('get_review_status', {'task_id': task_a}))

            dump('10. submit_review', await session.call_tool('submit_review', {
                'task_id': task_a, 'decision': 'approved',
                'reviewer_id': 'clinical-reviewer-1', 'comment': 'e2e smoke review',
            }))

            sc = dump('11. diagnose_heart_failure(B)', await session.call_tool('diagnose_heart_failure', {'case_id': case_id}))
            task_b = require(sc, 'task_id')
            sc_b = dump('12a. get_diagnosis_result(B)', await session.call_tool('get_diagnosis_result', {'task_id': task_b}))
            status_b = require(sc_b, 'status')

            if status_a == 'completed' and status_b == 'completed':
                dump('12b. compare_diagnoses', await session.call_tool('compare_diagnoses', {
                    'case_id': case_id, 'task_id_a': task_a, 'task_id_b': task_b,
                }))
            else:
                print()
                print('[skip compare] status_a=%s status_b=%s (not both completed; rerun later)' % (status_a, status_b))

            print()
            print('E2E SMOKE PASSED  case_id=%s  task_a=%s  task_b=%s' % (case_id, task_a, task_b))
            return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))