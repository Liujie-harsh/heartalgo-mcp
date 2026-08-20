"""ECG-FM runner 测试。

覆盖:
  - run(): XML → MAT → 推理 → Top-K (中文标签) + measurements + patient_info
  - parse_ecg_xml(): HL7 aECG XML 测量值 + 患者信息解析
  - 多 ECG 拒绝 (每任务仅允许 1 个 ECG)
  - ECGInputError: 非 XML 文件 / 文件不存在
"""
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from api import ImgItem
from ecgfm_runner import ECGFMRunner, parse_ecg_xml, ECGInputError


# ────────────────── run() 集成 (mock subprocess) ──────────────────

def test_run_returns_probability_sorted_top_k_with_chinese_labels(tmp_path, monkeypatch):
    """run(): XML → MAT → 推理 → Top-K 中文标签 + measurements + patient_info。"""
    project_dir = tmp_path / "ecg-fm"
    scripts = project_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    checkpoint = tmp_path / "weights.pt"
    checkpoint.touch()
    xml = tmp_path / "sample.xml"
    xml.write_text("<xml />", encoding="utf-8")

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1].endswith("infer_quickstart.py"):
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{
                "Atrial fibrillation": 0.9123,
                "Sinus rhythm": 0.0831,
                "Infarction": 0.2456,
            }], index=[str(tmp_path / "run" / "mat" / "sample.mat")]).to_csv(
                output_dir / "predictions_aggregated.csv"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("ecgfm_runner.subprocess.run", fake_run)
    runner = ECGFMRunner(
        project_dir=project_dir,
        checkpoint=checkpoint,
        python_executable="ecg-python",
        top_k=2,
        work_root=tmp_path / "run",
    )

    result = runner.run([ImgItem(imgId="ecg-1", imgPath=str(xml), imgType="ECG")])

    assert len(calls) == 2
    assert calls[0][0] == "ecg-python"
    assert calls[0][1].endswith("xml_to_ecgfm_mat.py")
    assert calls[1][1].endswith("infer_quickstart.py")
    # Top-K 按概率降序, 中文标签 (LABELS_ZH)
    assert result["ecg_predictions"] == {
        "ecg-1": [
            {"label": "心房颤动", "probability": 0.9123},
            {"label": "心肌梗死", "probability": 0.2456},
        ]
    }
    # parse_ecg_xml 对 fake XML <xml /> 提取不到字段:
    # measurements 返回空 dict, patient_info 返回 patientId/age/sex 均为 None
    assert result["ecg_measurements"] == {"ecg-1": {}}
    assert result["ecg_patient_info"] == {"ecg-1": {"patientId": None, "age": None, "sex": None}}


def test_run_empty_ecg_returns_empty_dicts(tmp_path):
    """无 ECG 输入 → 返回空 predictions/measurements/patient_info。"""
    runner = ECGFMRunner(
        project_dir=tmp_path,
        checkpoint=tmp_path / "w.pt",
        python_executable="py",
    )
    result = runner.run([])

    assert result == {"ecg_predictions": {}, "ecg_measurements": {}, "ecg_patient_info": {}}


def test_run_rejects_multiple_ecg(tmp_path):
    """多 ECG 拒绝: 每任务仅允许 1 个 ECG (handoff L300)。"""
    runner = ECGFMRunner(
        project_dir=tmp_path,
        checkpoint=tmp_path / "w.pt",
        python_executable="py",
    )
    imgs = [
        ImgItem(imgId="ecg-1", imgPath=str(tmp_path / "a.xml"), imgType="ECG"),
        ImgItem(imgId="ecg-2", imgPath=str(tmp_path / "b.xml"), imgType="ECG"),
    ]

    with pytest.raises(ValueError, match="exactly one ECG"):
        runner.run(imgs)


def test_run_rejects_non_xml_file(tmp_path):
    """非 XML 文件 → ECGInputError。"""
    project_dir = tmp_path / "ecg-fm"
    scripts = project_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    (tmp_path / "weights.pt").touch()

    runner = ECGFMRunner(
        project_dir=project_dir,
        checkpoint=tmp_path / "weights.pt",
        python_executable="py",
    )
    non_xml = tmp_path / "ecg.jpg"
    non_xml.touch()

    with pytest.raises(ECGInputError, match="XML"):
        runner.run([ImgItem(imgId="ecg-1", imgPath=str(non_xml), imgType="ECG")])


def test_run_rejects_missing_file(tmp_path):
    """文件不存在 → ECGInputError。"""
    project_dir = tmp_path / "ecg-fm"
    scripts = project_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    (tmp_path / "weights.pt").touch()

    runner = ECGFMRunner(
        project_dir=project_dir,
        checkpoint=tmp_path / "weights.pt",
        python_executable="py",
    )

    with pytest.raises(ECGInputError, match="不存在") as captured:
        runner.run([ImgItem(imgId="ecg-1", imgPath=str(tmp_path / "missing.xml"), imgType="ECG")])

    assert str(captured.value) == "ECG 输入文件不存在"
    assert str(tmp_path) not in str(captured.value)


# ────────────────── parse_ecg_xml ──────────────────

def test_parse_ecg_xml_returns_measurements_and_patient(tmp_path):
    """parse_ecg_xml: 解析 HL7 aECG XML 提取测量值 + 患者信息。"""
    xml = tmp_path / "a.xml"
    xml.write_text(
        '<AnnotatedECG xmlns="urn:hl7-org:v3">'
        '<effectiveTime><low value="20260731"/></effectiveTime>'
        '<subjectDemographicPerson>'
        '<administrativeGenderCode code="M"/>'
        '<birthTime value="19540406"/>'
        '</subjectDemographicPerson>'
        '<annotation><code code="MDC_ECG_HEART_RATE"/><value value="63"/></annotation>'
        '</AnnotatedECG>',
        encoding='utf-8'
    )
    m, p = parse_ecg_xml(xml)
    assert m["ventRate"] == 63
    # 当前代码提取 patientId/age/sex, 不解析 name
    assert p["age"] == 72
    assert p["sex"] == "M"
    assert "patientId" in p


def test_parse_ecg_xml_empty_xml_returns_empty(tmp_path):
    """空 XML → measurements={}, patient_info 全 None。"""
    xml = tmp_path / "empty.xml"
    xml.write_text("<xml />", encoding="utf-8")

    m, p = parse_ecg_xml(xml)

    assert m == {}
    assert p["patientId"] is None
    assert p["age"] is None
    assert p["sex"] is None


def test_parse_ecg_xml_extracts_all_measurement_codes(tmp_path):
    """parse_ecg_xml: 8 项测量值 (CODES 映射) 全部解析。"""
    xml = tmp_path / "full.xml"
    xml.write_text(
        '<AnnotatedECG xmlns="urn:hl7-org:v3">'
        '<effectiveTime><low value="20260731"/></effectiveTime>'
        '<annotation><code code="MDC_ECG_HEART_RATE"/><value value="63"/></annotation>'
        '<annotation><code code="MDC_ECG_TIME_PD_PR"/><value value="172"/></annotation>'
        '<annotation><code code="MDC_ECG_TIME_PD_QRS"/><value value="86"/></annotation>'
        '<annotation><code code="MDC_ECG_TIME_PD_QT"/><value value="380"/></annotation>'
        '<annotation><code code="MDC_ECG_TIME_PD_QTC"/><value value="389"/></annotation>'
        '<annotation><code code="MDC_ECG_ANGLE_P_FRONT"/><value value="60"/></annotation>'
        '<annotation><code code="MDC_ECG_ANGLE_QRS_FRONT"/><value value="48"/></annotation>'
        '<annotation><code code="MDC_ECG_ANGLE_T_FRONT"/><value value="45"/></annotation>'
        '</AnnotatedECG>',
        encoding='utf-8'
    )

    m, _ = parse_ecg_xml(xml)

    assert m == {
        "ventRate": 63, "prInterval": 172, "qrsDuration": 86,
        "qt": 380, "qtc": 389, "pAxis": 60, "qrsAxis": 48, "tAxis": 45,
    }
