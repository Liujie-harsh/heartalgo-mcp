"""ECG-FM 健康检查测试。

覆盖两条路径:
  - health_check_from_env(): 静态方法, 读 ECGFM_* 环境变量 (同事原测试)
  - health_check(): 实例方法, 读 self.config (Q1 决策新增, main.py 实际使用)
"""
import sys
from pathlib import Path

from config import ECGFMConfig
from ecgfm_runner import ECGFMRunner


def test_health_check_from_env_returns_healthy(tmp_path, monkeypatch):
    """静态 health_check_from_env: 路径齐全时返回 healthy (兼容同事原测试)。"""
    project = tmp_path / "ecg-fm"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"weights")
    monkeypatch.setenv("ECGFM_PROJECT_DIR", str(project))
    monkeypatch.setenv("ECGFM_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv("ECGFM_PYTHON", sys.executable)

    result = ECGFMRunner.health_check_from_env()

    assert result["status"] == "healthy"
    assert result["checkpointSizeBytes"] == 7
    assert result["pythonCallable"] is True
    assert result["pythonVersion"].startswith("Python")
    assert result["errors"] == []
    # env 工作流专属字段
    assert result["projectDirConfigured"] is True
    assert result["checkpointConfigured"] is True
    assert result["pythonConfigured"] is True


def test_health_check_from_env_reports_missing(tmp_path, monkeypatch):
    """静态 health_check_from_env: 环境变量缺失时返回 unhealthy + errors。"""
    monkeypatch.delenv("ECGFM_PROJECT_DIR", raising=False)
    monkeypatch.delenv("ECGFM_CHECKPOINT", raising=False)
    monkeypatch.delenv("ECGFM_PYTHON", raising=False)

    result = ECGFMRunner.health_check_from_env()

    assert result["status"] == "unhealthy"
    assert result["projectDirConfigured"] is False
    assert result["checkpointConfigured"] is False
    assert result["pythonConfigured"] is False
    assert any("ECGFM_PROJECT_DIR" in e for e in result["errors"])
    assert any("ECGFM_CHECKPOINT" in e for e in result["errors"])
    assert any("ECGFM_PYTHON" in e for e in result["errors"])


def test_instance_health_check_reads_runner_config(tmp_path):
    """实例 health_check: 读 self.config (Q1 决策), 不依赖环境变量。

    这是 main.py 实际使用的路径: 构造 ECGFMConfig -> ECGFMRunner -> runner.health_check。
    """
    project = tmp_path / "ecg-fm"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"weights")

    config = ECGFMConfig.from_cli(
        project_dir=project,
        checkpoint=checkpoint,
        python_executable=sys.executable,
    )
    runner = ECGFMRunner(config=config)
    result = runner.health_check()

    assert result["status"] == "healthy"
    assert result["projectDirExists"] is True
    assert result["converterScriptExists"] is True
    assert result["inferenceScriptExists"] is True
    assert result["checkpointExists"] is True
    assert result["checkpointSizeBytes"] == 7
    assert result["pythonCallable"] is True
    assert result["pythonVersion"].startswith("Python")
    assert result["errors"] == []


def test_instance_health_check_reports_missing_files(tmp_path):
    """实例 health_check: 配置的路径不存在时返回 unhealthy + 具体错误。"""
    # 不创建任何文件, 直接构造 config (from_cli 不校验存在性)
    config = ECGFMConfig.from_cli(
        project_dir=tmp_path / "missing-project",
        checkpoint=tmp_path / "missing.pt",
        python_executable=tmp_path / "missing-python.exe",
    )
    runner = ECGFMRunner(config=config)
    result = runner.health_check()

    assert result["status"] == "unhealthy"
    assert result["projectDirExists"] is False
    assert result["checkpointExists"] is False
    assert result["pythonExists"] is False
    assert result["pythonCallable"] is False
    assert any("ECGFM_PROJECT_DIR does not exist" in e for e in result["errors"])
    assert any("ECGFM_CHECKPOINT does not exist" in e for e in result["errors"])
    assert any("ECGFM_PYTHON does not exist" in e for e in result["errors"])


def test_instance_health_check_independent_of_env(monkeypatch, tmp_path):
    """实例 health_check: 即使环境变量全清空, 仍能基于 config 返回 healthy (Q1 关键差异)。"""
    project = tmp_path / "ecg-fm"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "xml_to_ecgfm_mat.py").touch()
    (scripts / "infer_quickstart.py").touch()
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"weights")

    # 清空所有 ECGFM_* 环境变量, 模拟 main.py 用 CLI 参数启动的场景
    monkeypatch.delenv("ECGFM_PROJECT_DIR", raising=False)
    monkeypatch.delenv("ECGFM_CHECKPOINT", raising=False)
    monkeypatch.delenv("ECGFM_PYTHON", raising=False)

    config = ECGFMConfig.from_cli(
        project_dir=project,
        checkpoint=checkpoint,
        python_executable=sys.executable,
    )
    runner = ECGFMRunner(config=config)

    # 静态方法会因环境变量缺失而 unhealthy
    env_result = ECGFMRunner.health_check_from_env()
    assert env_result["status"] == "unhealthy"

    # 实例方法仍能基于 config 返回 healthy (Q1 修复目标)
    instance_result = runner.health_check()
    assert instance_result["status"] == "healthy"
