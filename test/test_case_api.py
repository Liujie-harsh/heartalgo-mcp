"""病例上传到结构化诊断报告的公共 API 闭环测试。"""

import asyncio
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import main
from api import FakeRunner, create_app
from case_api import install_case_routes
from case_store import CaseConflictError, FileCaseStore
from mcp_server import build_mcp


def test_user_can_upload_echo_and_ecg_then_get_structured_report(tmp_path):
    runner = FakeRunner(metrics={
        "lvef": 35.48,
        "lvedd": 55.0,
        "lvesd": 40.0,
        "lad": 35.0,
        "mv_ea": 2.02,
        "ecg_predictions": {
            "asset-ecg": [{"label": "窦性心律", "probability": 0.86}],
        },
        "ecg_measurements": {"asset-ecg": {"ventRate": 63}},
        "ecg_patient_info": {"asset-ecg": {"age": 72, "sex": "M"}},
    })
    app = create_app(runner=runner, sync=True)
    install_case_routes(
        app,
        FileCaseStore(tmp_path / "cases"),
        service_user_id="heart-agent-service",
    )

    with TestClient(app) as client:
        created = client.post(
            "/heart-algo/cases",
            json={"requestId": "create-1", "sysUserId": "doctor-1"},
        )
        assert created.status_code == 201
        case_id = created.json()["caseId"]

        echo = client.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={
                "sysUserId": "doctor-1",
                "modality": "CARDIAC_ULTRASOUND",
                "dcmType": "PLAX",
                "assetId": "asset-echo",
            },
            files={"file": ("ignored-patient-name.dcm", b"\0" * 128 + b"DICM")},
        )
        assert echo.status_code == 201
        assert echo.json()["assetId"] == "asset-echo"

        ecg = client.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={
                "sysUserId": "doctor-1",
                "modality": "ECG",
                "assetId": "asset-ecg",
            },
            files={"file": ("ignored-patient-name.xml", b"<ECG><Lead>I</Lead></ECG>")},
        )
        assert ecg.status_code == 201

        submitted = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={"requestId": "diagnose-1", "sysUserId": "doctor-1"},
        )
        assert submitted.status_code == 202
        task_id = submitted.json()["taskId"]

        result = client.get(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}",
            params={"sys_user_id": "doctor-1"},
        )

        review = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}/review",
            json={
                "sysUserId": "doctor-1",
                "reviewerId": "cardiologist-7",
                "decision": "approved",
                "comment": "已核对原始资料与测量结果",
            },
        )
        reviewed_result = client.get(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}",
            params={"sys_user_id": "doctor-1"},
        )

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "completed"
    assert body["caseId"] == case_id
    assert body["taskId"] == task_id
    assert body["hfType"] == "HFrEF"
    assert body["cardiacUltrasound"][0]["dcmId"] == "asset-echo"
    assert body["cardiacUltrasound"][0]["measurements"]["lvef"]["value"] == 35.48
    assert body["ecg"][0]["ecgId"] == "asset-ecg"
    assert body["ecg"][0]["predictions"][0]["label"] == "窦性心律"
    assert review.status_code == 201
    assert review.json()["decision"] == "approved"
    assert reviewed_result.json()["requiresClinicianReview"] is False
    assert reviewed_result.json()["review"]["reviewerId"] == "cardiologist-7"


def test_mcp_submits_an_uploaded_case_and_returns_structured_result(tmp_path):
    app = create_app(
        runner=FakeRunner(metrics={
            "lvef": 35.48,
            "lvedd": 55.0,
            "lvesd": 40.0,
            "lad": 35.0,
            "mv_ea": 2.02,
        }),
        sync=True,
    )
    install_case_routes(
        app,
        FileCaseStore(tmp_path / "cases"),
        service_user_id="heart-agent-service",
    )
    with TestClient(app) as http:
        case_id = http.post(
            "/heart-algo/cases",
            json={"requestId": "create-mcp", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        uploaded = http.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={
                "sysUserId": "doctor-1",
                "modality": "CARDIAC_ULTRASOUND",
                "dcmType": "PLAX",
                "assetId": "asset-mcp-echo",
            },
            files={"file": ("echo.dcm", b"\0" * 128 + b"DICM")},
        )
        assert uploaded.status_code == 201

    mcp = build_mcp(app, service_user_id="heart-agent-service")

    async def run_client():
        async with Client(mcp, raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} >= {
                "diagnose_heart_failure",
                "get_diagnosis_result",
                "list_supported_views",
            }
            submitted = await client.call_tool(
                "diagnose_heart_failure",
                {"case_id": case_id},
            )
            assert submitted.is_error is False
            task_id = submitted.structured_content["task_id"]
            assert task_id.startswith("mcp-")
            result = await client.call_tool(
                "get_diagnosis_result",
                {"task_id": task_id},
            )
            views = await client.call_tool("list_supported_views", {})
            prompts = await client.list_prompts()
            templates = await client.list_resource_templates()
            resource = await client.read_resource(
                f"heart-algo://diagnosis/{task_id}"
            )
            assert views.is_error is False
            assert views.structured_content["views"][0]["dcm_type"] == "PLAX"
            assert "lvef" in views.structured_content["metrics"]
            assert any(prompt.name == "heart_failure_interpretation" for prompt in prompts.prompts)
            assert templates.resource_templates
            assert resource.contents
            assert '"reports"' in resource.contents[0].text
            assert "dcmPath" not in resource.contents[0].text
            return result

    result = asyncio.run(run_client())
    assert result.is_error is False
    assert result.structured_content["status"] == "completed"
    assert result.structured_content["hf_type"] == "HFrEF"
    assert result.structured_content["requires_clinician_review"] is True


def test_production_app_mounts_case_api_and_mcp_endpoint(tmp_path):
    app = main.build_app(
        use_fake=True,
        case_storage_root=str(tmp_path / "cases"),
        mcp_enabled=True,
    )

    with TestClient(app) as client:
        portal = client.get("/heart-algo/portal")
        created = client.post(
            "/heart-algo/cases",
            json={"requestId": "main-create", "sysUserId": "doctor-1"},
        )

    assert portal.status_code == 200
    assert "非 Agent" in portal.text
    assert "临床复核" in portal.text
    assert "纯 CPU 的 PLAX" in portal.text
    assert "请勿停止服务" in portal.text
    assert "关闭本页只会停止进度显示" in portal.text
    assert "不会取消后台任务" in portal.text
    assert "已等待" in portal.text
    assert created.status_code == 201
    assert hasattr(app.state, "mcp_server")
    assert any(route.path == "/mcp" for route in app.routes)


def test_diagnosis_request_id_is_idempotent_within_its_case(tmp_path):
    app = create_app(
        runner=FakeRunner(metrics={
            "lvef": 35.48,
            "lvedd": 55.0,
            "lvesd": 40.0,
            "lad": 35.0,
            "mv_ea": 2.02,
        }),
        sync=True,
    )
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_ids = [
            client.post(
                "/heart-algo/cases",
                json={"requestId": f"create-{index}", "sysUserId": "doctor-1"},
            ).json()["caseId"]
            for index in (1, 2)
        ]
        for index, case_id in enumerate(case_ids, start=1):
            uploaded = client.post(
                f"/heart-algo/cases/{case_id}/assets",
                data={
                    "sysUserId": "doctor-1",
                    "modality": "CARDIAC_ULTRASOUND",
                    "dcmType": "PLAX",
                    "assetId": f"asset-{index}",
                },
                files={"file": ("echo.dcm", b"\0" * 128 + b"DICM")},
            )
            assert uploaded.status_code == 201

        first = client.post(
            f"/heart-algo/cases/{case_ids[0]}/diagnoses",
            json={"requestId": "same-request", "sysUserId": "doctor-1"},
        ).json()
        retried = client.post(
            f"/heart-algo/cases/{case_ids[0]}/diagnoses",
            json={"requestId": "same-request", "sysUserId": "doctor-1"},
        ).json()
        other_case = client.post(
            f"/heart-algo/cases/{case_ids[1]}/diagnoses",
            json={"requestId": "same-request", "sysUserId": "doctor-1"},
        ).json()

    assert retried["taskId"] == first["taskId"]
    assert retried["created"] is False
    assert other_case["taskId"] != first["taskId"]


def test_diagnosis_retry_rejects_different_asset_selection(tmp_path):
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            json={"requestId": "create-conflict", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        for asset_id in ("asset-a", "asset-b"):
            assert client.post(
                f"/heart-algo/cases/{case_id}/assets",
                data={"sysUserId": "doctor-1", "modality": "ECG", "assetId": asset_id},
                files={"file": ("ecg.xml", b"<ECG />")},
            ).status_code == 201
        first = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={
                "requestId": "same-request",
                "sysUserId": "doctor-1",
                "assetIds": ["asset-a"],
            },
        )
        conflicting = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={
                "requestId": "same-request",
                "sysUserId": "doctor-1",
                "assetIds": ["asset-b"],
            },
        )

    assert first.status_code == 202
    assert conflicting.status_code == 409


def test_upload_can_use_a_server_generated_asset_id(tmp_path):
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            json={"requestId": "create-generated", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        uploaded = client.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={
                "sysUserId": "doctor-1",
                "modality": "ECG",
            },
            files={"file": ("patient-name.xml", b"<ECG />")},
        )

    assert uploaded.status_code == 201
    assert uploaded.json()["assetId"].startswith("asset-")
    assert "patient-name" not in uploaded.text


def test_invalid_upload_is_rejected_and_case_is_owner_isolated(tmp_path):
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            json={"requestId": "secure-case", "sysUserId": "doctor-a"},
        ).json()["caseId"]
        invalid = client.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={
                "sysUserId": "doctor-a",
                "modality": "CARDIAC_ULTRASOUND",
                "dcmType": "PLAX",
            },
            files={"file": ("fake.dcm", b"not-a-dicom")},
        )
        hidden = client.get(
            f"/heart-algo/cases/{case_id}",
            params={"sys_user_id": "doctor-b"},
        )
        owner_view = client.get(
            f"/heart-algo/cases/{case_id}",
            params={"sys_user_id": "doctor-a"},
        )

    assert invalid.status_code == 422
    assert hidden.status_code == 404
    assert owner_view.json()["assets"] == []


def test_provenance_and_review_are_scoped_to_each_diagnosis(tmp_path):
    app = create_app(
        runner=FakeRunner(metrics={
            "lvef": 35.48,
            "lvedd": 55.0,
            "lvesd": 40.0,
            "lad": 35.0,
            "mv_ea": 2.02,
        }),
        sync=True,
    )
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            json={"requestId": "scoped-case", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        for asset_id in ("asset-first", "asset-second"):
            client.post(
                f"/heart-algo/cases/{case_id}/assets",
                data={
                    "sysUserId": "doctor-1",
                    "modality": "CARDIAC_ULTRASOUND",
                    "dcmType": "PLAX",
                    "assetId": asset_id,
                },
                files={"file": ("echo.dcm", b"\0" * 128 + b"DICM")},
            )
        first_task = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={
                "requestId": "first-task",
                "sysUserId": "doctor-1",
                "assetIds": ["asset-first"],
            },
        ).json()["taskId"]
        client.post(
            f"/heart-algo/cases/{case_id}/diagnoses/{first_task}/review",
            json={
                "sysUserId": "doctor-1",
                "reviewerId": "reviewer-1",
                "decision": "approved",
            },
        )
        second_task = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={
                "requestId": "second-task",
                "sysUserId": "doctor-1",
                "assetIds": ["asset-second"],
            },
        ).json()["taskId"]
        second_result = client.get(
            f"/heart-algo/cases/{case_id}/diagnoses/{second_task}",
            params={"sys_user_id": "doctor-1"},
        ).json()

    assert set(second_result["inputs"]) == {"asset-second"}
    assert second_result["review"] is None
    assert second_result["requiresClinicianReview"] is True


def test_algorithm_version_is_captured_when_inference_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("ALGORITHM_VERSION", "echo-2026.08")
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            json={"requestId": "version-case", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        client.post(
            f"/heart-algo/cases/{case_id}/assets",
            data={"sysUserId": "doctor-1", "modality": "ECG", "assetId": "ecg-1"},
            files={"file": ("ecg.xml", b"<ECG />")},
        )
        task_id = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            json={"requestId": "version-task", "sysUserId": "doctor-1"},
        ).json()["taskId"]
        monkeypatch.setenv("ALGORITHM_VERSION", "echo-2026.09")
        result = client.get(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}",
            params={"sys_user_id": "doctor-1"},
        ).json()

    assert result["algorithmVersion"] == "echo-2026.08"


def test_authenticated_case_api_rejects_caller_identity_override(tmp_path):
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(
        app,
        FileCaseStore(tmp_path / "cases"),
        require_authenticated_user=True,
        trusted_proxy_secret="gateway-secret",
    )

    with TestClient(app) as client:
        missing = client.post(
            "/heart-algo/cases",
            json={"requestId": "auth-case", "sysUserId": "doctor-1"},
        )
        forged = client.post(
            "/heart-algo/cases",
            headers={
                "X-Authenticated-User": "doctor-2",
                "X-Auth-Proxy-Secret": "gateway-secret",
            },
            json={"requestId": "auth-case", "sysUserId": "doctor-1"},
        )
        valid = client.post(
            "/heart-algo/cases",
            headers={
                "X-Authenticated-User": "doctor-1",
                "X-Auth-Proxy-Secret": "gateway-secret",
            },
            json={"requestId": "auth-case", "sysUserId": "doctor-1"},
        )

    assert missing.status_code == 401
    assert forged.status_code == 403
    assert valid.status_code == 201


def test_authenticated_independent_cardiologist_can_review(tmp_path):
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(
        app,
        FileCaseStore(tmp_path / "cases"),
        require_authenticated_user=True,
        trusted_proxy_secret="gateway-secret",
    )
    owner_headers = {
        "X-Authenticated-User": "doctor-1",
        "X-Auth-Proxy-Secret": "gateway-secret",
    }
    reviewer_headers = {
        "X-Authenticated-User": "cardiologist-7",
        "X-Authenticated-Roles": "cardiology-reviewer",
        "X-Auth-Proxy-Secret": "gateway-secret",
    }

    with TestClient(app) as client:
        case_id = client.post(
            "/heart-algo/cases",
            headers=owner_headers,
            json={"requestId": "review-case", "sysUserId": "doctor-1"},
        ).json()["caseId"]
        client.post(
            f"/heart-algo/cases/{case_id}/assets",
            headers=owner_headers,
            data={"sysUserId": "doctor-1", "modality": "ECG", "assetId": "ecg-1"},
            files={"file": ("ecg.xml", b"<ECG />")},
        )
        task_id = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses",
            headers=owner_headers,
            json={"requestId": "review-task", "sysUserId": "doctor-1"},
        ).json()["taskId"]
        review_view = client.get(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}",
            headers=reviewer_headers,
            params={"sys_user_id": "doctor-1"},
        )
        reviewed = client.post(
            f"/heart-algo/cases/{case_id}/diagnoses/{task_id}/review",
            headers=reviewer_headers,
            json={
                "sysUserId": "doctor-1",
                "reviewerId": "cardiologist-7",
                "decision": "approved",
            },
        )

    assert review_view.status_code == 200
    assert review_view.json()["taskId"] == task_id
    assert reviewed.status_code == 201
    assert reviewed.json()["reviewerId"] == "cardiologist-7"


def test_startup_recovers_case_submission_reserved_before_task_creation(tmp_path):
    case_store = FileCaseStore(tmp_path / "cases")
    metadata, _ = case_store.create_case("doctor-1", "recovery-case")
    asset, _ = case_store.add_asset(
        metadata["caseId"],
        "doctor-1",
        "ecg-recovery",
        "ECG",
        None,
        BytesIO(b"<ECG />"),
    )
    diagnosis, _ = case_store.reserve_diagnosis(
        metadata["caseId"],
        "doctor-1",
        "diagnosis-recovery",
        "recovery-request",
        [asset["assetId"]],
    )
    app = create_app(runner=FakeRunner(metrics={}), sync=True)
    install_case_routes(app, case_store)

    with TestClient(app) as client:
        result = client.get(
            f"/heart-algo/cases/{metadata['caseId']}/diagnoses/{diagnosis['taskId']}",
            params={"sys_user_id": "doctor-1"},
        )

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert case_store.get_case(metadata["caseId"], "doctor-1")["diagnoses"][0][
        "submissionState"
    ] == "submitted"


def test_case_storage_rejects_a_second_production_instance(tmp_path):
    first = FileCaseStore(tmp_path / "cases")
    second = FileCaseStore(tmp_path / "cases")
    first.acquire_instance_lock()
    try:
        with pytest.raises(CaseConflictError, match="另一个算法服务实例"):
            second.acquire_instance_lock()
    finally:
        first.close_instance_lock()
