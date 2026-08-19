"""病例上传到结构化诊断报告的公共 API 闭环测试。"""

import asyncio

from fastapi.testclient import TestClient
from mcp import Client

import main
from api import FakeRunner, create_app
from case_api import install_case_routes
from case_store import FileCaseStore
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
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))

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
    install_case_routes(app, FileCaseStore(tmp_path / "cases"))
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

    mcp = build_mcp(app)

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
                {
                    "case_id": case_id,
                    "sys_user_id": "doctor-1",
                    "request_id": "mcp-diagnose-1",
                },
            )
            assert submitted.is_error is False
            task_id = submitted.structured_content["taskId"]
            result = await client.call_tool(
                "get_diagnosis_result",
                {
                    "case_id": case_id,
                    "task_id": task_id,
                    "sys_user_id": "doctor-1",
                },
            )
            return result

    result = asyncio.run(run_client())
    assert result.is_error is False
    assert result.structured_content["status"] == "completed"
    assert result.structured_content["hfType"] == "HFrEF"
    assert result.structured_content["requiresClinicianReview"] is True


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
