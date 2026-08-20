import json
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "deepseek-harness-plugin"


def test_bundle_manifest_is_installable_by_deepseek_harness():
    manifest = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "heart-algo-dsh-plugin"
    assert manifest["type"] == "module"
    assert manifest["files"] == ["cordis.patch.yml", "README.md"]
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"


def test_bundle_connects_the_heart_mcp_server_without_embedded_credentials():
    patch = (PLUGIN_DIR / "cordis.patch.yml").read_text(encoding="utf-8")

    assert "- insert:" in patch
    assert "id: heart-algo-mcp" in patch
    assert "name: '@deepseek-ai/dsh-mcp-client'" in patch
    assert "serverName: heart-algo" in patch
    assert "transport: streamable-http" in patch
    assert "HEART_ALGO_MCP_URL" in patch
    assert "http://127.0.0.1:8000/mcp" in patch
    assert "HEART_ALGO_MCP_TOKEN" in patch
    assert "toolCallTimeoutMs: 130000" in patch
    assert "failOnStartupError: true" in patch


def test_bundle_documents_install_start_and_expected_tools():
    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")

    assert "dsh plugin --profile web add" in readme
    assert "MCP_ENABLED=true" in readme
    assert "MCP_SHARED_SECRET" in readme
    assert "mcp__heart-algo__diagnose_heart_failure" in readme
    assert "mcp__heart-algo__get_diagnosis_result" in readme
