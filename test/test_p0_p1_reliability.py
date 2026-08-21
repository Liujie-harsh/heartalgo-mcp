from __future__ import annotations

import json
import subprocess
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from algorithm_version import AlgorithmVersionError, resolve_algorithm_version
from api import FakeRunner, create_app
from case_store import CaseStoreError, FileCaseStore
from config import MeasurementConfig
from echonet_runner import EchoNetRunner
from quality.physical_delta import (
    CalibrationMeasurement,
    PhysicalDeltaError,
    UltrasoundRegion,
    build_calibration_report,
    calibrate_measurement,
    extract_ultrasound_regions,
)
from quality.model_coverage import audit_model_coverage
from quality.cpu_stability import BenchmarkScenario, run_stability_benchmark
from quality.release_identity import build_release_manifest
from metric_catalog import VIEW_METRICS
from task_models import ImgItem


def _case_store(tmp_path):
    store = FileCaseStore(tmp_path / "cases")
    case, _ = store.create_case("doctor-1", "create-1")
    return store, case["caseId"]


def _write_checkpoint(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("checkpoint/data.pkl", b"model")


def _write_dicom(path):
    path.write_bytes(b"\0" * 128 + b"DICM" + b"metadata")


def test_ecg_upload_accepts_utf8_bom(tmp_path):
    store, case_id = _case_store(tmp_path)

    asset, created = store.add_asset(
        case_id,
        "doctor-1",
        "ecg-bom",
        "ECG",
        None,
        BytesIO(b"\xef\xbb\xbf<AnnotatedECG xmlns='urn:hl7-org:v3'/>")
    )

    assert created is True
    assert asset["sizeBytes"] > 3


def test_ecg_upload_rejects_malformed_xml(tmp_path):
    store, case_id = _case_store(tmp_path)

    with pytest.raises(CaseStoreError, match="XML"):
        store.add_asset(
            case_id,
            "doctor-1",
            "ecg-truncated",
            "ECG",
            None,
            BytesIO(b"<AnnotatedECG>"),
        )

    assert not (store.root / case_id / "assets" / "ecg-truncated.xml").exists()


def test_ecg_upload_rejects_dtd_and_entity_declarations(tmp_path):
    store, case_id = _case_store(tmp_path)
    xml = (
        b'<!DOCTYPE AnnotatedECG [<!ENTITY payload "expanded">]>'
        b"<AnnotatedECG>&payload;</AnnotatedECG>"
    )

    with pytest.raises(CaseStoreError, match="DTD|实体"):
        store.add_asset(
            case_id,
            "doctor-1",
            "ecg-doctype",
            "ECG",
            None,
            BytesIO(xml),
        )


def test_ecg_upload_rejects_well_formed_non_ecg_xml(tmp_path):
    store, case_id = _case_store(tmp_path)

    with pytest.raises(CaseStoreError, match="ECG XML"):
        store.add_asset(
            case_id,
            "doctor-1",
            "not-ecg",
            "ECG",
            None,
            BytesIO(b"<html><body>not an ECG</body></html>"),
        )


def test_real_service_requires_explicit_algorithm_version():
    with pytest.raises(AlgorithmVersionError, match="ALGORITHM_VERSION"):
        resolve_algorithm_version({}, use_fake=False)


def test_fake_app_exposes_stable_algorithm_version(monkeypatch, tmp_path):
    monkeypatch.delenv("ALGORITHM_VERSION", raising=False)

    app = main.build_app(use_fake=True, case_storage_root=str(tmp_path / "cases"))
    try:
        assert app.state.algorithm_version == "fake"
    finally:
        app.state.task_queue.close(wait=True)


def test_explicit_app_algorithm_version_is_written_to_task_result():
    app = create_app(
        runner=FakeRunner(metrics={}),
        sync=True,
        algorithm_version="heart@abc+models@0123456789abcdef",
    )

    with TestClient(app) as client:
        client.post(
            "/heart-algo/task/start",
            json={
                "requestId": "version-request",
                "sysUserId": "doctor-1",
                "taskId": "version-task",
                "ecg": [{"ecgId": "ecg-1", "ecgPath": "ignored.xml"}],
            },
        )
        result = client.post(
            "/heart-algo/task/result",
            json={
                "requestId": "version-result",
                "sysUserId": "doctor-1",
                "taskId": "version-task",
            },
        ).json()

    payload = json.loads(result["reports"][0]["reportResult"])
    assert payload["algorithmVersion"] == "heart@abc+models@0123456789abcdef"


def test_physical_delta_calibration_reports_gold_standard_error():
    region = UltrasoundRegion(
        index=0,
        min_x=0,
        max_x=640,
        min_y=0,
        max_y=480,
        unit_x_code=3,
        unit_y_code=3,
        delta_x=0.01,
        delta_y=0.01,
    )
    measurement = CalibrationMeasurement(
        name="lvid",
        points=((100, 50), (400, 450)),
        axis="euclidean",
        gold_standard=5.1,
        gold_standard_unit="cm",
        absolute_tolerance=0.2,
    )

    result = calibrate_measurement([region], measurement)

    assert result["convertedValue"] == pytest.approx(5.0)
    assert result["unit"] == "cm"
    assert result["absoluteError"] == pytest.approx(0.1)
    assert result["relativeErrorPercent"] == pytest.approx(1.960784, rel=1e-5)
    assert result["withinTolerance"] is True
    assert result["rawScale"]["physicalDeltaX"] == 0.01


def test_extracts_physical_delta_from_dicom_ultrasound_region_sequence():
    dataset = SimpleNamespace(
        SequenceOfUltrasoundRegions=[
            SimpleNamespace(
                RegionLocationMinX0=10,
                RegionLocationMaxX1=630,
                RegionLocationMinY0=20,
                RegionLocationMaxY1=470,
                PhysicalUnitsXDirection=4,
                PhysicalUnitsYDirection=7,
                PhysicalDeltaX=0.002,
                PhysicalDeltaY=0.5,
                ReferencePixelX0=320,
                ReferencePixelY0=300,
                ReferencePixelPhysicalValueX=0.0,
                ReferencePixelPhysicalValueY=0.0,
            )
        ]
    )

    regions = extract_ultrasound_regions(dataset)

    assert regions == [
        UltrasoundRegion(
            index=0,
            min_x=10,
            max_x=630,
            min_y=20,
            max_y=470,
            unit_x_code=4,
            unit_y_code=7,
            delta_x=0.002,
            delta_y=0.5,
            reference_pixel_x=320,
            reference_pixel_y=300,
            reference_value_x=0.0,
            reference_value_y=0.0,
        )
    ]


def test_physical_delta_requires_region_index_when_regions_overlap():
    regions = [
        UltrasoundRegion(0, 0, 640, 0, 480, 3, 3, 0.01, 0.01),
        UltrasoundRegion(1, 0, 640, 0, 480, 4, 7, 0.01, 0.5),
    ]
    measurement = CalibrationMeasurement(
        name="ambiguous",
        points=((100, 100), (200, 200)),
        axis="y",
    )

    with pytest.raises(PhysicalDeltaError, match="regionIndex"):
        calibrate_measurement(regions, measurement)


def test_physical_delta_rejects_negative_clinical_tolerance():
    region = UltrasoundRegion(0, 0, 640, 0, 480, 3, 3, 0.01, 0.01)
    measurement = CalibrationMeasurement(
        name="lvid",
        points=((100, 100), (200, 100)),
        axis="x",
        gold_standard=1.0,
        gold_standard_unit="cm",
        absolute_tolerance=-0.1,
    )

    with pytest.raises(PhysicalDeltaError, match="容差"):
        calibrate_measurement([region], measurement)


def test_physical_delta_absolute_y_uses_reference_pixel_and_value():
    region = UltrasoundRegion(
        0,
        0,
        1080,
        0,
        426,
        4,
        7,
        0.002,
        -0.5,
        reference_pixel_y=300,
        reference_value_y=10.0,
    )
    measurement = CalibrationMeasurement(
        name="tr_vmax",
        points=((540, 220),),
        axis="absolute_y",
        gold_standard=50.0,
        gold_standard_unit="cm/s",
        absolute_tolerance=0.1,
    )

    result = calibrate_measurement([region], measurement)

    assert result["convertedValue"] == pytest.approx(50.0)
    assert result["withinTolerance"] is True


def test_calibration_report_is_deidentified_and_summarizes_acceptance():
    regions = [
        UltrasoundRegion(0, 0, 640, 0, 480, 3, 3, 0.01, 0.01)
    ]
    manifest = {
        "algorithmVersion": "heart@abc+measurement@weights-1",
        "dcmType": "PLAX",
        "measurements": [
            {
                "name": "lvid",
                "points": [[0, 0], [300, 400]],
                "axis": "euclidean",
                "goldStandard": 5.1,
                "goldStandardUnit": "cm",
                "absoluteTolerance": 0.2,
            },
            {
                "name": "la",
                "points": [[0, 0], [100, 0]],
                "axis": "x",
                "goldStandard": 2.0,
                "goldStandardUnit": "cm",
                "absoluteTolerance": 0.2,
            },
        ],
    }

    report = build_calibration_report(
        regions=regions,
        source_sha256="a" * 64,
        manifest=manifest,
    )

    assert report["sourceSha256"] == "a" * 64
    assert report["algorithmVersion"] == manifest["algorithmVersion"]
    assert report["dcmType"] == "PLAX"
    assert report["summary"] == {
        "total": 2,
        "withGoldStandard": 2,
        "passed": 1,
        "failed": 1,
        "unevaluated": 0,
    }
    assert "patient" not in str(report).lower()


def test_calibration_report_rejects_measurement_without_clinical_tolerance():
    regions = [UltrasoundRegion(0, 0, 640, 0, 480, 3, 3, 0.01, 0.01)]
    manifest = {
        "algorithmVersion": "heart@abc+models@0123456789abcdef",
        "dcmType": "PLAX",
        "measurements": [
            {
                "name": "lvid",
                "points": [[0, 0], [100, 0]],
                "axis": "x",
                "goldStandard": 1.0,
                "goldStandardUnit": "cm",
            }
        ],
    }

    with pytest.raises(PhysicalDeltaError, match="容差"):
        build_calibration_report(
            regions=regions,
            source_sha256="a" * 64,
            manifest=manifest,
        )


def test_calibration_report_rejects_metric_from_another_view():
    regions = [UltrasoundRegion(0, 0, 640, 0, 480, 3, 3, 0.01, 0.01)]
    manifest = {
        "algorithmVersion": "heart@abc+models@0123456789abcdef",
        "dcmType": "PLAX",
        "measurements": [
            {
                "name": "tr_vmax",
                "points": [[0, 0], [100, 0]],
                "axis": "x",
                "goldStandard": 1.0,
                "goldStandardUnit": "cm",
                "absoluteTolerance": 0.1,
            }
        ],
    }

    with pytest.raises(PhysicalDeltaError, match="不属于切面"):
        build_calibration_report(
            regions=regions,
            source_sha256="a" * 64,
            manifest=manifest,
        )


def test_model_coverage_requires_scripts_weights_and_sample_for_each_view(tmp_path):
    measurement = tmp_path / "Measurement"
    samples = tmp_path / "samples"
    (measurement / "weights" / "Doppler_models").mkdir(parents=True)
    (measurement / "inference_Doppler_image.py").touch()
    _write_checkpoint(
        measurement / "weights" / "Doppler_models" / "trvmax_weights.ckpt"
    )
    (samples / "TR_Vmax").mkdir(parents=True)
    _write_dicom(samples / "TR_Vmax" / "sample.dcm")

    report = audit_model_coverage(measurement, samples)

    tr_vmax = next(item for item in report["views"] if item["dcmType"] == "TR_Vmax")
    plax = next(item for item in report["views"] if item["dcmType"] == "PLAX")
    assert tr_vmax["assetsReady"] is True
    assert plax["assetsReady"] is False
    assert "ivs_weights.ckpt" in report["summary"]["missingWeights"]
    assert "PLAX" in report["summary"]["missingSampleViews"]


def test_model_coverage_rejects_git_lfs_pointer_as_missing_weight(tmp_path):
    measurement = tmp_path / "Measurement"
    samples = tmp_path / "samples"
    (measurement / "weights" / "Doppler_models").mkdir(parents=True)
    (measurement / "inference_Doppler_image.py").touch()
    pointer = measurement / "weights" / "Doppler_models" / "trvmax_weights.ckpt"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "a" * 64 + "\nsize 123456\n",
        encoding="ascii",
    )
    (samples / "TR_Vmax").mkdir(parents=True)
    _write_dicom(samples / "TR_Vmax" / "sample.dcm")

    report = audit_model_coverage(measurement, samples)
    tr_vmax = next(item for item in report["views"] if item["dcmType"] == "TR_Vmax")

    assert tr_vmax["assetsReady"] is False
    assert "trvmax_weights.ckpt" in report["summary"]["lfsPointerWeights"]


def test_model_coverage_rejects_random_weight_and_non_dicom_bytes(tmp_path):
    measurement = tmp_path / "Measurement"
    samples = tmp_path / "samples"
    (measurement / "weights" / "Doppler_models").mkdir(parents=True)
    (measurement / "inference_Doppler_image.py").touch()
    (measurement / "weights" / "Doppler_models" / "trvmax_weights.ckpt").write_bytes(
        b"not-a-checkpoint"
    )
    (samples / "TR_Vmax").mkdir(parents=True)
    (samples / "TR_Vmax" / "sample.dcm").write_bytes(b"not-a-dicom")

    report = audit_model_coverage(measurement, samples)
    tr_vmax = next(item for item in report["views"] if item["dcmType"] == "TR_Vmax")

    assert tr_vmax["assetsReady"] is False
    assert tr_vmax["validDicomCount"] == 0
    assert "trvmax_weights.ckpt" in report["summary"]["invalidWeights"]


def test_cpu_stability_report_tracks_continuous_success_without_input_data(tmp_path):
    class Runner:
        def __init__(self):
            self.calls = 0

        def run(self, imgs, task_id="", work_root=None):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("private path C:/patient/sample.xml")
            echo_ids = [item.imgId for item in imgs if item.imgType == "CARDIAC_ULTRASOUND"]
            ecg_ids = [item.imgId for item in imgs if item.imgType == "ECG"]
            return {
                "patientId": "must-not-enter-report",
                "echo_per_image": {item: {"tr_vmax": 64.0} for item in echo_ids},
                "ecg_predictions": {item: [{"label": "x", "probability": 1.0}] for item in ecg_ids},
            }

    scenarios = [
        BenchmarkScenario(
            "ecg",
            (ImgItem(imgId="ecg-1", imgPath="C:/patient/sample.xml", imgType="ECG"),),
        ),
        BenchmarkScenario(
            "echo",
            tuple(
                ImgItem(
                    imgId=f"echo-{dcm_type}",
                    imgPath="C:/patient/sample.dcm",
                    imgType="CARDIAC_ULTRASOUND",
                    dcmType=dcm_type,
                )
                for dcm_type in VIEW_METRICS
            ),
        ),
        BenchmarkScenario(
            "mixed",
            (
                ImgItem(
                    imgId="echo-mixed",
                    imgPath="C:/patient/sample.dcm",
                    imgType="CARDIAC_ULTRASOUND",
                    dcmType="TR_Vmax",
                ),
                ImgItem(
                    imgId="ecg-mixed",
                    imgPath="C:/patient/sample.xml",
                    imgType="ECG",
                ),
            ),
        ),
    ]

    report = run_stability_benchmark(
        Runner(),
        scenarios,
        iterations=2,
        work_root=tmp_path,
        algorithm_version="heart@abc+measurement@weights-1",
        max_run_seconds=5.0,
    )

    assert report["summary"]["runs"] == 6
    assert report["summary"]["succeeded"] == 5
    assert report["summary"]["failed"] == 1
    assert report["summary"]["successRate"] == pytest.approx(5 / 6)
    assert {item["kind"] for item in report["scenarios"]} == {"ecg", "echo", "mixed"}
    assert "C:/patient" not in str(report)
    assert "must-not-enter-report" not in str(report)


def test_cpu_stability_rejects_scenario_that_would_run_no_model(tmp_path):
    scenario = BenchmarkScenario(
        "invalid",
        (ImgItem(imgId="bad", imgPath="ignored", imgType="UNKNOWN"),),
    )

    with pytest.raises(ValueError, match="imgType"):
        run_stability_benchmark(
            object(),
            [scenario],
            iterations=1,
            work_root=tmp_path,
            algorithm_version="heart@abc+models@0123456789abcdef",
            max_run_seconds=5.0,
        )


def test_cpu_stability_requires_ecg_echo_and_mixed_scenarios(tmp_path):
    scenario = BenchmarkScenario(
        "ecg-only",
        (ImgItem(imgId="ecg", imgPath="ignored.xml", imgType="ECG"),),
    )

    with pytest.raises(ValueError, match="ECG、心超和混合"):
        run_stability_benchmark(
            object(),
            [scenario],
            iterations=1,
            work_root=tmp_path,
            algorithm_version="heart@abc+models@0123456789abcdef",
            max_run_seconds=5.0,
        )


def test_cpu_stability_requires_every_supported_echo_view(tmp_path):
    scenarios = [
        BenchmarkScenario(
            "ecg",
            (ImgItem(imgId="ecg", imgPath="ignored.xml", imgType="ECG"),),
        ),
        BenchmarkScenario(
            "echo",
            (
                ImgItem(
                    imgId="echo",
                    imgPath="ignored.dcm",
                    imgType="CARDIAC_ULTRASOUND",
                    dcmType="TR_Vmax",
                ),
            ),
        ),
        BenchmarkScenario(
            "mixed",
            (
                ImgItem(
                    imgId="mixed-echo",
                    imgPath="ignored.dcm",
                    imgType="CARDIAC_ULTRASOUND",
                    dcmType="TR_Vmax",
                ),
                ImgItem(imgId="mixed-ecg", imgPath="ignored.xml", imgType="ECG"),
            ),
        ),
    ]

    with pytest.raises(ValueError, match="逐切面"):
        run_stability_benchmark(
            object(),
            scenarios,
            iterations=1,
            work_root=tmp_path,
            algorithm_version="heart@abc+models@0123456789abcdef",
            max_run_seconds=5.0,
        )


def test_echo_runner_turns_subprocess_timeout_into_per_image_error(
    tmp_path, monkeypatch
):
    runner = EchoNetRunner(
        config=MeasurementConfig(
            script_dir=tmp_path,
            python_executable="python",
            timeout_seconds=1,
        )
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="measurement", timeout=1)

    monkeypatch.setattr("echonet_runner.subprocess.run", timeout)

    result = runner.run(
        [
            ImgItem(
                imgId="echo-timeout",
                imgPath="input.dcm",
                imgType="CARDIAC_ULTRASOUND",
                dcmType="TR_Vmax",
            )
        ],
        task_id="timeout-task",
        work_root=str(tmp_path),
    )

    assert "超时" in result["echo_per_image"]["echo-timeout"]["error"]


def test_release_identity_hashes_model_artifacts_without_embedding_paths(tmp_path):
    echo_weight = tmp_path / "lvid_weights.ckpt"
    ecg_weight = tmp_path / "mimic_iv_ecg_finetuned.pt"
    echo_weight.write_bytes(b"echo-weights")
    ecg_weight.write_bytes(b"ecg-weights")

    manifest = build_release_manifest(
        "3c1464f",
        {"measurement-lvid": echo_weight, "ecgfm-mimic17": ecg_weight},
    )

    assert manifest["algorithmVersion"].startswith("heart@3c1464f+models@")
    assert set(manifest["artifacts"]) == {"measurement-lvid", "ecgfm-mimic17"}
    assert len(manifest["artifacts"]["measurement-lvid"]["sha256"]) == 64
    assert str(tmp_path) not in str(manifest)


def test_release_identity_rejects_git_lfs_pointer(tmp_path):
    pointer = tmp_path / "lvid_weights.ckpt"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "a" * 64 + "\nsize 123456\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="Git LFS"):
        build_release_manifest("3c1464f", {"measurement-lvid": pointer})
