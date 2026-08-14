"""
心衰诊断 API 测试 (异步任务模式, v3 接口协议)。

遵循《图像算法分析接口协议 v3.1》:
  POST /heart-algo/task/start  → 启动分析任务, 返回 taskState
  POST /heart-algo/task/result → 查询任务结果

v3 请求体按切面分组:
  cardiacUltrasound: [{dcmType, dcms:[{dcmId, dcmPath}]}]
  ecg: [{ecgId, ecgPath}]

v3 响应体心超与 ECG 分离:
  cardiacUltrasound: [{dcmId, dcmPath, reportId, rois:[{roiType, points}]}]
  ecg: [{ecgId, ecgPath, reportId}]
  reports: [{reportId, reportType, reportResult(JSON 字符串)}]
  reportType: CU-SUB (心超 per-img) / CU-SUMMARY (心超汇总) / ECG

测试策略:
  推理层抽象为 runner (注入), 测试用 FakeRunner (无需 torch/GPU)。
  sync 模式下 start 同步执行完毕, 测试确定性高。
"""
import json

import pytest
from fastapi.testclient import TestClient

from api import create_app, FakeRunner
from ecgfm_runner import ECGConversionError
from task_models import ImgItem
from task_store import InMemoryTaskStore


@pytest.fixture
def fake_echo_runner():
    """返回固定心超指标的假推理器 (本机无 torch 也能跑)。

    v3: ea → mv_ea, gls 已删除 (不再返回 null)。
    """
    return FakeRunner(metrics={
        "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0,
        "lad": 35.0, "mv_ea": 2.02,
    })


@pytest.fixture
def fake_mixed_runner():
    """心超 + ECG 混合假推理器。"""
    return FakeRunner(metrics={
        "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0,
        "lad": 35.0, "mv_ea": 2.02,
        "ecg_predictions": {"ecg-1": [{"label": "窦性心律", "probability": 0.86}]},
        "ecg_measurements": {"ecg-1": {"ventRate": 63}},
        "ecg_patient_info": {"ecg-1": {"patientId": "P001", "age": 72, "sex": "M"}},
    })


@pytest.fixture
def client(fake_echo_runner):
    """sync 模式 app: start 同步执行完, 测试确定性高。"""
    app = create_app(runner=fake_echo_runner, sync=True)
    return TestClient(app)


@pytest.fixture
def mixed_client(fake_mixed_runner):
    """sync 模式 app (心超+ECG 混合)。"""
    app = create_app(runner=fake_mixed_runner, sync=True)
    return TestClient(app)


def _v3_start_body(
    task_id="task-1",
    dcm_type="PLAX",
    request_id="req-1",
    user_id="user-1",
):
    """构造 v3 start 请求体 (仅心超)。"""
    return {
        "requestId": request_id,
        "sysUserId": user_id,
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


def _v3_mixed_start_body(task_id="task-mixed", request_id="req-mixed"):
    """构造 v3 start 请求体 (心超 + ECG)。"""
    return {
        "requestId": request_id,
        "sysUserId": "user-1",
        "taskId": task_id,
        "cardiacUltrasound": [
            {
                "dcmType": "PLAX",
                "dcms": [
                    {"dcmId": "echo-1", "dcmPath": "/data/echo.dcm"}
                ],
            }
        ],
        "ecg": [
            {"ecgId": "ecg-1", "ecgPath": "/data/ecg.xml"}
        ],
    }


def test_app_startup_redelivers_persisted_queued_tasks(fake_echo_runner):
    store = InMemoryTaskStore()
    store.create_or_get(
        "task-recovered",
        [
            ImgItem(
                imgId="img-recovered",
                imgPath="/data/recovered.dcm",
                imgType="CARDIAC_ULTRASOUND",
                dcmType="PLAX",
            )
        ],
        request_id="req-recovered",
        sys_user_id="user-1",
    )
    app = create_app(
        runner=fake_echo_runner,
        sync=False,
        store=store,
        stale_running_seconds=3600,
    )

    with TestClient(app):
        app.state.task_queue.join()

    assert store.get("task-recovered")["taskState"] == 2


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

    def test_task_id_idempotent(self, client):
        # 同 taskId 重发 (不同 requestId) → 命中 taskId 幂等检查, 返回已有状态, 不重复启动
        client.post("/heart-algo/task/start", json=_v3_start_body())
        resp = client.post("/heart-algo/task/start", json=_v3_start_body(request_id="req-2"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["taskState"] == 2
        assert "already exists" in body["resultMsg"]

    def test_request_id_idempotent(self, client):
        # 同 requestId 重发 (不同 taskId) → 返回已有任务
        client.post("/heart-algo/task/start", json=_v3_start_body(task_id="t-a", request_id="req-shared"))
        resp = client.post("/heart-algo/task/start", json=_v3_start_body(task_id="t-b", request_id="req-shared"))
        assert resp.status_code == 200
        body = resp.json()
        # 返回首个任务的 taskId, 不创建 t-b
        assert body["taskId"] == "t-a"

    def test_request_id_is_unique_per_user(self, client):
        # 同 requestId、不同 sysUserId → 分别创建任务
        first = client.post(
            "/heart-algo/task/start",
            json=_v3_start_body(
                task_id="t-user-a",
                request_id="req-shared",
                user_id="user-a",
            ),
        )
        second = client.post(
            "/heart-algo/task/start",
            json=_v3_start_body(
                task_id="t-user-b",
                request_id="req-shared",
                user_id="user-b",
            ),
        )

        assert first.json()["taskId"] == "t-user-a"
        assert second.json()["taskId"] == "t-user-b"

    def test_other_user_cannot_reuse_existing_task_id(self, client):
        client.post(
            "/heart-algo/task/start",
            json=_v3_start_body(
                task_id="task-shared",
                request_id="request-a",
                user_id="user-a",
            ),
        )

        resp = client.post(
            "/heart-algo/task/start",
            json=_v3_start_body(
                task_id="task-shared",
                request_id="request-b",
                user_id="user-b",
            ),
        )

        body = resp.json()
        assert body["resultCode"] == 1
        assert body["taskState"] == 3
        assert "task id conflict" in body["resultMsg"]

    def test_failed_reason_excluded_when_success(self, client):
        # response_model_exclude_none: 成功时 failedReason 字段不出现
        resp = client.post("/heart-algo/task/start", json=_v3_start_body())
        body = resp.json()
        assert "failedReason" not in body

    def test_unexpected_runner_error_is_not_exposed(self):
        class BrokenRunner:
            def run(self, imgs, task_id="", work_root=None):
                raise RuntimeError(
                    "stderr: password=secret C:/private/model.py line 99"
                )

        app = create_app(runner=BrokenRunner(), sync=True)
        with TestClient(app) as failed_client:
            failed_client.post("/heart-algo/task/start", json=_v3_start_body())
            response = failed_client.post(
                "/heart-algo/task/result",
                json={
                    "requestId": "req-result",
                    "sysUserId": "user-1",
                    "taskId": "task-1",
                },
            )

        body = response.json()
        serialized = response.text
        assert body["failedReason"] == "算法服务内部错误，请联系管理员"
        assert "secret" not in serialized
        assert "C:/private" not in serialized

    def test_known_runner_error_keeps_its_safe_message(self):
        class InvalidEcgRunner:
            def run(self, imgs, task_id="", work_root=None):
                raise ECGConversionError(
                    "ECG 输入不完整：十二导联采样点数量不一致"
                )

        app = create_app(runner=InvalidEcgRunner(), sync=True)
        with TestClient(app) as failed_client:
            response = failed_client.post(
                "/heart-algo/task/start",
                json=_v3_start_body(),
            )

        assert response.json()["resultMsg"] == "ECG 输入不完整：十二导联采样点数量不一致"


# ────────────────── result 端点 ──────────────────

class TestResultTask:
    """POST /heart-algo/task/result 查询任务结果。"""

    def _start_task(self, client, task_id="task-1"):
        """辅助: 启动一个任务。"""
        return client.post("/heart-algo/task/start", json=_v3_start_body(task_id))

    def test_returns_cu_sub_report_when_done(self, client):
        # sync 模式: start 后任务已完成, result 应返回 taskState=2 + CU-SUB 报告
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
        assert report["reportType"] == "CU-SUB"
        payload = json.loads(report["reportResult"])
        assert payload["dcmId"] == "img-1"
        assert "lvef" in payload["measurements"]
        assert "hf_type" in payload["measurements"]
        assert payload["measurements"]["hf_type"] == "HFrEF"  # LVEF=35.48 → HFrEF
        # v3: cardiacUltrasound 数组
        assert len(body["cardiacUltrasound"]) == 1
        assert body["cardiacUltrasound"][0]["dcmId"] == "img-1"

    def test_cu_summary_report_appended(self, client):
        # 有心超指标时, 顶层 reports 追加 CU-SUMMARY 汇总报告
        self._start_task(client)
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        body = resp.json()
        report_types = [r["reportType"] for r in body["reports"]]
        assert "CU-SUB" in report_types
        assert "CU-SUMMARY" in report_types
        # CU-SUMMARY 不关联 cardiacUltrasound[]
        summary_report = next(r for r in body["reports"] if r["reportType"] == "CU-SUMMARY")
        summary_payload = json.loads(summary_report["reportResult"])
        assert "measurements" in summary_payload
        assert summary_payload["measurements"]["hf_type"] == "HFrEF"
        assert "gls" not in summary_payload["measurements"]

    def test_measurements_include_metadata(self, client):
        # measurements 含中文名/单位/参考范围 (METRIC_META)
        self._start_task(client)
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        body = resp.json()
        cu_sub = next(r for r in body["reports"] if r["reportType"] == "CU-SUB")
        payload = json.loads(cu_sub["reportResult"])
        lvef_meta = payload["measurements"]["lvef"]
        assert "value" in lvef_meta
        assert lvef_meta["name_cn"] == "左室射血分数(EF)"
        assert lvef_meta["unit"] == "%"

    def test_unknown_task_returns_failed(self, client):
        # 查询不存在的任务 → taskState=3 + failedReason
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-3", "sysUserId": "user-1", "taskId": "nonexistent",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["taskState"] == 3
        assert body["failedReason"] is not None

    def test_other_user_cannot_read_task_result(self, client):
        self._start_task(client)

        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-other",
            "sysUserId": "other-user",
            "taskId": "task-1",
        })

        body = resp.json()
        assert body["resultCode"] == 1
        assert body["taskState"] == 3
        assert body["reports"] == []
        assert "task not found" in body["failedReason"]

    def test_failed_reason_excluded_when_success(self, client):
        # response_model_exclude_none: 成功时 failedReason 字段不出现
        self._start_task(client)
        resp = client.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        body = resp.json()
        assert "failedReason" not in body


# ────────────────── 心超 + ECG 混合任务 ──────────────────

class TestMixedTask:
    """心超 + ECG 混合任务的 v3 响应结构。"""

    def test_mixed_response_has_echo_and_ecg_sections(self, mixed_client):
        # v3: cardiacUltrasound + ecg 两个顶层数组分离
        mixed_client.post("/heart-algo/task/start", json=_v3_mixed_start_body())
        resp = mixed_client.post("/heart-algo/task/result", json={
            "requestId": "req-r", "sysUserId": "user-1", "taskId": "task-mixed",
        })
        body = resp.json()
        assert body["taskState"] == 2
        assert len(body["cardiacUltrasound"]) == 1
        assert len(body["ecg"]) == 1
        assert body["ecg"][0]["ecgId"] == "ecg-1"

    def test_mixed_reports_include_all_types(self, mixed_client):
        # 混合任务 reports 含 CU-SUB + CU-SUMMARY + ECG
        mixed_client.post("/heart-algo/task/start", json=_v3_mixed_start_body())
        resp = mixed_client.post("/heart-algo/task/result", json={
            "requestId": "req-r", "sysUserId": "user-1", "taskId": "task-mixed",
        })
        body = resp.json()
        report_types = [r["reportType"] for r in body["reports"]]
        assert "CU-SUB" in report_types
        assert "CU-SUMMARY" in report_types
        assert "ECG" in report_types

    def test_ecg_report_payload_structure(self, mixed_client):
        # ECG report 的 reportResult JSON 含 ecgId/patientInfo/measurements/predictions
        mixed_client.post("/heart-algo/task/start", json=_v3_mixed_start_body())
        resp = mixed_client.post("/heart-algo/task/result", json={
            "requestId": "req-r", "sysUserId": "user-1", "taskId": "task-mixed",
        })
        body = resp.json()
        ecg_report = next(r for r in body["reports"] if r["reportType"] == "ECG")
        payload = json.loads(ecg_report["reportResult"])
        assert payload["ecgId"] == "ecg-1"
        assert "patientInfo" in payload
        assert "measurements" in payload
        assert "predictions" in payload
        assert payload["predictions"][0]["label"] == "窦性心律"


# ────────────────── rois 结构 ──────────────────

class TestRois:
    """心超 rois 结构 (v3: roiType 用指标名, points 为坐标点列表)。"""

    def test_echo_rois_from_per_image(self):
        # echo_per_image 含 rois 时, 响应 rois 按 {roiType, points} 结构返回
        runner = FakeRunner(metrics={
            "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0, "lad": 35.0, "mv_ea": 2.02,
            "echo_per_image": {
                "img-1": {
                    "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0, "lad": 35.0,
                    "rois": [
                        {"type": "LVEDD", "frameIndex": 19, "points": [(388, 185), (305, 275)]},
                        {"type": "LVESD", "frameIndex": 54, "points": [(373, 191), (324, 248)]},
                    ],
                }
            },
        })
        app = create_app(runner=runner, sync=True)
        c = TestClient(app)
        c.post("/heart-algo/task/start", json=_v3_start_body())
        resp = c.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        body = resp.json()
        rois = body["cardiacUltrasound"][0]["rois"]
        assert len(rois) == 2
        assert rois[0]["roiType"] == "LVEDD"
        assert len(rois[0]["points"]) == 2
        assert rois[0]["points"][0] == {"xPos": 388, "yPos": 185}

    def test_doppler_rois_empty(self):
        # Doppler 切面 (MV_EA) 无 CSV 坐标 → rois 为空数组
        runner = FakeRunner(metrics={
            "mv_ea": 2.02,
            "echo_per_image": {"img-1": {"mv_e": 99.75, "mv_a": 49.37, "mv_ea": 2.02, "rois": []}},
        })
        app = create_app(runner=runner, sync=True)
        c = TestClient(app)
        c.post("/heart-algo/task/start", json=_v3_start_body(dcm_type="MV_EA"))
        resp = c.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "task-1",
        })
        body = resp.json()
        assert body["cardiacUltrasound"][0]["rois"] == []


# ────────────────── 推理失败 ──────────────────

class TestInferenceFailure:
    """推理失败时任务标记为失败。"""

    def test_runner_exception_marks_task_failed(self):
        # 未分类异常只返回稳定公共消息，原始细节留在服务日志
        failing_runner = FakeRunner(metrics={})
        failing_runner.run = lambda imgs, task_id="", work_root=None: (_ for _ in ()).throw(RuntimeError("DICOM 解析失败"))
        app = create_app(runner=failing_runner, sync=True)
        c = TestClient(app)

        c.post("/heart-algo/task/start", json=_v3_start_body(task_id="t1"))
        resp = c.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "t1",
        })
        body = resp.json()
        assert body["taskState"] == 3
        assert body["failedReason"] == "算法服务内部错误，请联系管理员"
        assert "DICOM 解析失败" not in resp.text

    def test_failure_payload_has_cu_sub_reports(self):
        # 失败时仍按文件返回 CU-SUB/ECG 报告 (含 error 字段)
        failing_runner = FakeRunner(metrics={})
        failing_runner.run = lambda imgs, task_id="", work_root=None: (_ for _ in ()).throw(RuntimeError("model crash"))
        app = create_app(runner=failing_runner, sync=True)
        c = TestClient(app)
        c.post("/heart-algo/task/start", json=_v3_start_body(task_id="t1"))
        resp = c.post("/heart-algo/task/result", json={
            "requestId": "req-2", "sysUserId": "user-1", "taskId": "t1",
        })
        body = resp.json()
        assert body["taskState"] == 3
        # 心超图仍有 CU-SUB 报告 (含 error)
        cu_sub = next(r for r in body["reports"] if r["reportType"] == "CU-SUB")
        payload = json.loads(cu_sub["reportResult"])
        assert "error" in payload
