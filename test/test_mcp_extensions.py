"""MCP 扩展工具闭环测试：一站式分析、规则解读、报告、检索、复核与对比。"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from mcp import Client

from api import FakeRunner, create_app
from case_api import install_case_routes
from case_store import FileCaseStore
from interpretation import (
    classify_lvef,
    compare_diagnosis_results,
    evaluate_metric,
    interpret_diagnosis_result,
    parse_reference,
)
from mcp_server import build_mcp
from report_render import render_markdown_report


SERVICE_USER = "heart-agent-service"

DICOM_BYTES = b"\0" * 128 + b"DICM"
ECG_BYTES = b"<ECG><Lead>I</Lead></ECG>"


def _make_tmp_dir(prefix: str) -> Path:
    """在沙盒/CI 环境下可用 HEART_TEST_TMPDIR 重定向临时根目录。"""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("HEART_TEST_TMPDIR") or None))


def _metrics(**overrides):
    metrics = {
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
    }
    metrics.update(overrides)
    return metrics


def _build(tmp_path, metrics=None):
    app = create_app(
        runner=FakeRunner(metrics=_metrics() if metrics is None else metrics),
        sync=True,
    )
    install_case_routes(
        app,
        FileCaseStore(tmp_path / "cases"),
        service_user_id=SERVICE_USER,
    )
    return app


def _analyze_files(tmp_path):
    echo_path = tmp_path / "echo.dcm"
    echo_path.write_bytes(DICOM_BYTES)
    ecg_path = tmp_path / "ecg.xml"
    ecg_path.write_bytes(ECG_BYTES)
    return [
        {
            "path": str(echo_path),
            "modality": "CARDIAC_ULTRASOUND",
            "dcm_type": "PLAX",
            "asset_id": "asset-echo",
        },
        {
            "path": str(ecg_path),
            "modality": "ECG",
            "asset_id": "asset-ecg",
        },
    ]


# ────────────────── interpretation 纯函数 ──────────────────


def test_parse_reference_supports_catalog_formats():
    assert parse_reference("20–37") == (20.0, 37.0)
    assert parse_reference("≤280") == (None, 280.0)
    assert parse_reference("≥17") == (17.0, None)
    assert parse_reference("—") == (None, None)
    assert parse_reference(None) == (None, None)


def test_classify_lvef_boundaries_match_protocol():
    assert classify_lvef(35.48) == "HFrEF"
    assert classify_lvef(40.0) == "HFmrEF"
    assert classify_lvef(49.9) == "HFmrEF"
    assert classify_lvef(50.0) == "HFpEF"
    assert classify_lvef(None) is None


def test_evaluate_metric_flags_low_high_and_normal():
    assert evaluate_metric("lvef", 35.48)["status"] == "low"
    assert evaluate_metric("ivs", 13.0)["status"] == "high"
    assert evaluate_metric("lvedd", 50.0)["status"] == "normal"
    assert evaluate_metric("unknown_metric", 1.0) is None


def test_interpret_derives_combined_indicators_and_flags():
    result = {
        "cardiac_ultrasound": [{
            "dcm_id": "asset-echo",
            "measurements": {
                "mv_e": {"value": 150.0, "unit": "cm/s"},
                "mv_a": {"value": 50.0, "unit": "cm/s"},
                "tdi_medial": {"value": 6.0, "unit": "cm/s"},
                "tdi_lateral": {"value": 8.0, "unit": "cm/s"},
                "lvef": {"value": 35.48, "unit": "%"},
            },
        }],
        "ecg": [],
    }
    analysis = interpret_diagnosis_result(result)
    assert analysis["lvef_classification"] == "HFrEF"
    combined = {item["name"]: item for item in analysis["combined_indicators"]}
    assert combined["E/A"]["value"] == 3.0
    assert combined["E/A"]["status"] == "high"
    assert combined["E/A"]["basis"].startswith("derived")
    assert combined["E/e'"]["value"] == 21.4286  # 150 / avg(6, 8)
    assert combined["E/e'"]["status"] == "high"
    assert any(
        finding["metric"] == "lvef" and finding["status"] == "low"
        for finding in analysis["abnormal_findings"]
    )
    assert any("Teichholz" in note for note in analysis["notes"])


def test_compare_results_computes_deltas_and_classification_change():
    def build(lvef, lvedd):
        return {
            "cardiac_ultrasound": [{
                "dcm_id": "asset-echo",
                "measurements": {
                    "lvef": {"value": lvef},
                    "lvedd": {"value": lvedd},
                },
            }],
            "ecg": [],
        }

    comparison = compare_diagnosis_results(build(35.0, 55.0), build(45.0, 52.0))
    rows = {row["metric"]: row for row in comparison["metrics"]}
    assert rows["lvef"]["delta"] == 10.0
    assert rows["lvef"]["notable"] is True
    assert rows["lvedd"]["direction"] == "decreased"
    assert rows["lvedd"]["notable"] is False  # -5.5% 低于 10% 阈值
    assert comparison["lvef_classification"] == {"from": "HFrEF", "to": "HFmrEF"}


# ────────────────── report_render ──────────────────


def test_report_render_includes_sections_and_disclaimers():
    contract = {
        "case_id": "case-1",
        "task_id": "mcp-1",
        "status": "completed",
        "hf_type": "HFrEF",
        "algorithm_version": "test-1",
        "requires_clinician_review": True,
        "review_status": "pending",
        "cardiac_ultrasound": [{
            "dcm_id": "asset-echo",
            "measurements": {
                "lvef": {
                    "value": 35.48,
                    "unit": "%",
                    "reference": "55–70",
                    "name_cn": "左室射血分数(EF)",
                },
            },
        }],
        "ecg": [{
            "ecg_id": "asset-ecg",
            "patient_info": {},
            "measurements": {"ventRate": 63},
            "predictions": [{"label": "窦性心律", "probability": 0.86}],
            "error": None,
        }],
        "inputs": {"asset-echo": {"sha256": "abc123", "sizeBytes": 132}},
    }
    markdown = render_markdown_report(contract, interpret_diagnosis_result(contract))
    assert "# 心衰辅助分析报告" in markdown
    assert "尚未经临床复核" in markdown
    assert "Teichholz" in markdown
    assert "| 35.48 | % | 55–70 | 偏低 |" in markdown
    assert "窦性心律" in markdown
    assert "abc123" in markdown


# ────────────────── MCP 工具闭环 ──────────────────


def test_one_stop_analysis_interpret_report_management_and_review():
    tmp_path = _make_tmp_dir("heart-mcp-ext-")
    try:
        _run_one_stop(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _run_one_stop(tmp_path):
    app = _build(tmp_path)
    mcp = build_mcp(app, service_user_id=SERVICE_USER)

    async def run_client():
        async with Client(mcp, raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} >= {
                "analyze_case_files",
                "interpret_diagnosis",
                "generate_report",
                "list_cases",
                "get_case_detail",
                "list_tasks",
                "get_review_status",
                "submit_review",
                "compare_diagnoses",
            }

            submitted = await client.call_tool("analyze_case_files", {
                "files": _analyze_files(tmp_path),
                "request_id": "mcp-batch-1",
            })
            body = submitted.structured_content
            case_id = body["case_id"]
            task_id = body["task_id"]
            assert case_id.startswith("case-")
            assert task_id.startswith("mcp-")
            assert body["status"] == "completed"
            assert len(body["assets"]) == 2

            result = await client.call_tool("get_diagnosis_result", {"task_id": task_id})
            assert result.structured_content["hf_type"] == "HFrEF"

            analysis = await client.call_tool("interpret_diagnosis", {"task_id": task_id})
            interpreted = analysis.structured_content
            assert interpreted["lvef_classification"] == "HFrEF"
            assert any(
                finding["metric"] == "lvef"
                for finding in interpreted["abnormal_findings"]
            )
            assert (
                interpreted["ecg_highlights"][0]["top_predictions"][0]["label"]
                == "窦性心律"
            )

            report = await client.call_tool("generate_report", {
                "task_id": task_id,
                "format": "markdown",
                "save_to_case": True,
            })
            assert "# 心衰辅助分析报告" in report.structured_content["content"]
            assert (
                report.structured_content["artifact"]["artifactId"]
                == f"report-{task_id}.md"
            )

            cases = await client.call_tool("list_cases", {})
            assert cases.structured_content["count"] == 1
            detail = await client.call_tool("get_case_detail", {"case_id": case_id})
            assert detail.structured_content["diagnoses"][0]["status"] == "completed"
            assert len(detail.structured_content["assets"]) == 2
            assert (
                detail.structured_content["artifacts"][0]["artifactId"]
                == f"report-{task_id}.md"
            )
            tasks = await client.call_tool("list_tasks", {})
            assert tasks.structured_content["count"] == 1
            assert tasks.structured_content["tasks"][0]["status"] == "completed"
            filtered = await client.call_tool("list_tasks", {"case_id": case_id})
            assert filtered.structured_content["count"] == 1

            reviewed = await client.call_tool("submit_review", {
                "task_id": task_id,
                "decision": "approved",
                "reviewer_id": "cardiologist-7",
                "comment": "已核对",
            })
            assert reviewed.structured_content["decision"] == "approved"
            status = await client.call_tool("get_review_status", {"task_id": task_id})
            assert status.structured_content["review_status"] == "approved"
            assert status.structured_content["requires_clinician_review"] is False
            assert status.structured_content["review_count"] == 1
            return task_id

    task_id = asyncio.run(run_client())

    async def run_error_cases():
        async with Client(mcp) as client:
            self_review = await client.call_tool("submit_review", {
                "task_id": task_id,
                "decision": "approved",
                "reviewer_id": SERVICE_USER,
            })
            assert self_review.is_error is True
            bad_file = tmp_path / "bad.dcm"
            bad_file.write_bytes(b"not a dicom file")
            rejected = await client.call_tool("analyze_case_files", {"files": [
                {
                    "path": str(bad_file),
                    "modality": "CARDIAC_ULTRASOUND",
                    "dcm_type": "PLAX",
                },
            ]})
            assert rejected.is_error is True
            missing = tmp_path / "missing.dcm"
            rejected = await client.call_tool("analyze_case_files", {"files": [
                {
                    "path": str(missing),
                    "modality": "CARDIAC_ULTRASOUND",
                    "dcm_type": "PLAX",
                },
            ]})
            assert rejected.is_error is True

    asyncio.run(run_error_cases())


def test_compare_diagnoses_tracks_two_tasks_on_same_case():
    tmp_path = _make_tmp_dir("heart-mcp-cmp-")
    try:
        _run_compare(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _run_compare(tmp_path):
    app = _build(tmp_path)
    mcp = build_mcp(app, service_user_id=SERVICE_USER)
    files = _analyze_files(tmp_path)

    async def run_client():
        async with Client(mcp, raise_exceptions=True) as client:
            first = await client.call_tool("analyze_case_files", {
                "files": files,
                "request_id": "mcp-followup-a",
            })
            case_id = first.structured_content["case_id"]
            task_a = first.structured_content["task_id"]

            app.state.runner._metrics["lvef"] = 45.0
            second = await client.call_tool("diagnose_heart_failure", {
                "case_id": case_id,
            })
            task_b = second.structured_content["task_id"]
            assert task_b != task_a

            comparison = await client.call_tool("compare_diagnoses", {
                "case_id": case_id,
                "task_id_a": task_a,
                "task_id_b": task_b,
            })
            rows = {
                row["metric"]: row
                for row in comparison.structured_content["comparison"]["metrics"]
            }
            assert rows["lvef"]["delta"] == 9.52
            assert rows["lvef"]["direction"] == "increased"
            assert rows["lvef"]["notable"] is True
            assert comparison.structured_content["comparison"][
                "lvef_classification"
            ] == {"from": "HFrEF", "to": "HFmrEF"}
            return case_id, task_a

    case_id, task_a = asyncio.run(run_client())

    async def run_cross_case_rejection():
        async with Client(mcp) as client:
            other = await client.call_tool("analyze_case_files", {
                "files": _analyze_files(tmp_path),
                "request_id": "mcp-followup-b",
            })
            stray = await client.call_tool("compare_diagnoses", {
                "case_id": case_id,
                "task_id_a": task_a,
                "task_id_b": other.structured_content["task_id"],
            })
            assert stray.is_error is True

    asyncio.run(run_cross_case_rejection())


def test_generate_report_json_without_saving():
    tmp_path = _make_tmp_dir("heart-mcp-rep-")
    try:
        _run_report_json(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _run_report_json(tmp_path):
    app = _build(tmp_path)
    mcp = build_mcp(app, service_user_id=SERVICE_USER)

    async def run_client():
        async with Client(mcp, raise_exceptions=True) as client:
            submitted = await client.call_tool("analyze_case_files", {
                "files": _analyze_files(tmp_path),
            })
            task_id = submitted.structured_content["task_id"]
            report = await client.call_tool("generate_report", {
                "task_id": task_id,
                "format": "json",
            })
            body = report.structured_content
            assert body["format"] == "json"
            assert '"hf_type"' in body["content"]
            assert "artifact" not in body
            bad_format = await client.call_tool("generate_report", {
                "task_id": task_id,
                "format": "pdf",
            })
            assert bad_format.is_error is True

    asyncio.run(run_client())
