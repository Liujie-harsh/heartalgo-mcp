from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from api import ImgItem
from ecgfm_runner import ECGFMRunner, parse_ecg_xml


def test_ecgfm_runner_converts_xml_and_returns_probability_sorted_top_k(tmp_path, monkeypatch):
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
    # 标签中文化 (LABELS_ZH)
    assert result == {
        "ecg_predictions": {
            "ecg-1": [
                {"label": "心房颤动", "probability": 0.9123},
                {"label": "心肌梗死", "probability": 0.2456},
            ]
        },
        # parse_ecg_xml 对 fake XML <xml /> 提取不到字段:
        # measurements 返回空 dict, patient_info 返回 3 个 None 默认值
        # run() 总会写入 imgId 键 (API 层 ECGMeasurements(**{}) 全 None, exclude_none 剔除)
        "ecg_measurements": {"ecg-1": {}},
        "ecg_patient_info": {"ecg-1": {"name": None, "age": None, "sex": None}},
    }


def test_parse_ecg_xml_returns_measurements_and_patient(tmp_path):
    """同事贡献: 解析 HL7 aECG XML 提取测量值 + 患者信息。"""
    xml = tmp_path / "a.xml"
    xml.write_text(
        '<AnnotatedECG xmlns="urn:hl7-org:v3">'
        '<effectiveTime><low value="20260731"/></effectiveTime>'
        '<subjectDemographicPerson>'
        '<name><family>梁</family><given>怀显</given></name>'
        '<administrativeGenderCode code="M"/>'
        '<birthTime value="19540406"/>'
        '</subjectDemographicPerson>'
        '<annotation><code code="MDC_ECG_HEART_RATE"/><value value="63"/></annotation>'
        '</AnnotatedECG>',
        encoding='utf-8'
    )
    m, p = parse_ecg_xml(xml)
    assert m["ventRate"] == 63
    assert p == {"name": "梁 怀显", "age": 72, "sex": "M"}
