"""
心衰诊断 API 测试 (异步任务模式, v3 接口协议)。

遵循《图像算法分析接口协议 v3》:
  POST /heart-algo/task/start  → 启动分析任务, 返回 taskState
  POST /heart-algo/task/result → 查询任务结果

v3 请求体按切面分组:
  cardiacUltrasound: [{dcmType, dcms:[{dcmId, dcmPath}]}]
  ecg: [{ecgId, ecgPath}]

v3 响应体心超与 ECG 分离:
  cardiacUltrasound: [{dcmId, dcmPath, reportId, rois}]
  ecg: [{ecgId, ecgPath, reportId}]
  reports: [{reportId, reportType, reportResult(JSON 字符串)}]

测试策略:
  推理层抽象为 runner (注入), 测试用 FakeRunner (无需 torch/GPU)。
  sync 模式下 start 同步执行完毕, 测试确定性高。
"""
import json

import pytest
from fastapi.testclient import TestClient

from api import create_app, FakeRunner


@pytest.fixture
def fake_runner():
    """返回固定 6 项指标的假推理器 (本机无 torch 也能跑)。"""
    return FakeRunner(metrics={
        "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0,
        "lad": 35.0, "ea": 2.02, "gls": None,
    })


@pytest.fixture
def client(fake_runner):
    """sync 模式 app: start 同步执行完, 测试确定性高。"""
    app = create_app(runner=fake_runner, sync=True)
    return TestClient(app)


def _v3_start_body(task_id="task-1", dcm_type="PLAX"):
    """构造 v3 start 请求体。"""
    return {
        "requestId": "req-1",
        "sysUserId": "user-1",
        "taskId": task_id,
        "cardiacUltrasound": [
            {
                "dcmType": dcm_type,
                "dcms": [
                    {"dcmId": "img-1", "dcmPath": "/data/sample.dcm"}
                ],
            }
        ],
    }


# ────────────────── start 端点 ──────────────────

class TestStartTask:
    """POST /heart-algo/task/start 启动任务。"""

    def test_returns_task_id_and_success_code(self, client):
        # 协议: 返回 taskId (与请求一致) + resultCode=0 (成功)
        resp = client.post("/heart-algo/task/start", json=_v3_start_body())
        assert resp.status_code == 200
        body = resp.json()
        assert body["taskId"] == "task-1"
        assert body["resultCode"] == 0
        # sync 模式: 启动后立即执行完毕 → taskState=2 (分析结束-成功)
        assert body["taskState"] == 2


# ────────────────── result 端点 ──────────────────

class TestResultTask:
    """POST /heart-algo/task/result 查询任务结果。"""

    def _start_task(self, client, task_id="task-1"):
        """辅助: 启动一个任务。"""
        return client.post("/heart-algo/task/start", json=_v3_start_body(task_id))

    def test_returns_metrics_and_hf_type_when_done(self, client):
        # sync 模式: start 后任务已完成, result 应返回 taskState=2 + 指标 + 分型
        self._start_task(client)
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["taskState"] == 2
        assert body["resultCode"] == 0
        # v3: reports 是 list[Report], reportResult 是 JSON 字符串
        assert len(body["reports"]) >= 1
        report = body["reports"][0]
        assert report["reportType"] == "MEASUREMENT"
        payload = json.loads(report["reportResult"])
        assert payload["dcmId"] == "img-1"
        assert "lvef" in payload["measurements"]
        assert "hf_type" in payload["measurements"]
        assert payload["measurements"]["hf_type"] == "HFrEF"  # LVEF=35.48 → HFrEF
        # v3: cardiacUltrasound 数组
        assert len(body["cardiacUltrasound"]) == 1
        assert body["cardiacUltrasound"][0]["dcmId"] == "img-1"

    def test_unknown_task_returns_failed(self, client):
        # 查询不存在的任务 → taskState=3 + failedReason
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-3", "sysUserId": "user-1", "taskId": "nonexistent",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["taskState"] == 3
        assert body["failedReason"] is not None


class TestInferenceFailure:
    """推理失败时任务标记为失败。"""

    def test_runner_exception_marks_task_failed(self):
        # runner 抛异常 → taskState=3 + failedReason 含错误信息
        failing_runner = FakeRunner(metrics={})
        failing_runner.run = lambda imgs, task_id="", work_root=None: (_ for _ in ()).throw(RuntimeError("DICOM 解析失败"))
        app = create_app(runner=failing_runner, sync=True)
        c = TestClient(app)

        c.post("/heart-algo/task/start", json=_v3_start_body(task_id="t1"))
        resp = c.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "u1", "taskId": "t1",
        })
        body = resp.json()
        assert body["taskState"] == 3
        assert "DICOM 解析失败" in body["failedReason"]
