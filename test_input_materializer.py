"""输入物化层的端到端行为测试。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from api import create_app
from combined_runner import CombinedRunner
from input_materializer import DownloadSettings, InputMaterializationError, InputMaterializer
from task_models import ImgItem


class _DownloadHandler(BaseHTTPRequestHandler):
    payload = b"downloaded-dicom"
    last_authorization: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler public hook
        type(self).last_authorization = self.headers.get("Authorization")
        if self.path == "/missing.dcm":
            self.send_error(404)
            return
        if self.path == "/redirect.dcm":
            self.send_response(302)
            self.send_header("Location", "/ok.dcm")
            self.end_headers()
            return
        payload = {
            "/first.dcm": b"first-dicom",
            "/second.dcm": b"second-dicom",
        }.get(self.path, self.payload)
        self.send_response(200)
        declared_length = len(payload) + 10 if self.path == "/truncated.dcm" else len(payload)
        self.send_header("Content-Length", str(declared_length))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class _CapturingEchoRunner:
    def __init__(self) -> None:
        self.seen_path: str | None = None

    def run(self, imgs, task_id="", work_root=None):
        self.seen_path = imgs[0].imgPath
        assert Path(self.seen_path).read_bytes() == _DownloadHandler.payload
        return {
            "lvef": 55.0,
            "lvedd": 50.0,
            "lvesd": 32.0,
            "lad": 34.0,
            "mv_ea": 1.2,
            "echo_per_image": {imgs[0].imgId: {"rois": []}},
        }


class _UnusedECGRunner:
    def run(self, imgs, task_id="", work_root=None):  # pragma: no cover
        raise AssertionError("ECG runner should not be called")


def test_http_input_is_materialized_but_result_keeps_original_reference(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        source_url = f"http://{authority}/studies/1.dcm"
        echo_runner = _CapturingEchoRunner()
        runner = CombinedRunner(
            echo_runner=echo_runner,
            ecg_runner=_UnusedECGRunner(),
            input_materializer=InputMaterializer(
                DownloadSettings(allowed_authorities=frozenset({authority}))
            ),
        )
        app = create_app(runner=runner, sync=True, work_root=str(tmp_path))

        with TestClient(app) as client:
            start = client.post("/heart-algo/task/start", json={
                "requestId": "request-download-1",
                "sysUserId": "user-1",
                "taskId": "task-download-1",
                "cardiacUltrasound": [{
                    "dcmType": "PLAX",
                    "dcms": [{"dcmId": "dcm-1", "dcmPath": source_url}],
                }],
                "ecg": [],
            })
            result = client.post("/heart-algo/task/result", json={
                "requestId": "result-download-1",
                "sysUserId": "user-1",
                "taskId": "task-download-1",
            })

        assert start.json()["taskState"] == 2
        assert result.json()["cardiacUltrasound"][0]["dcmPath"] == source_url
        assert echo_runner.seen_path == str(
            tmp_path / "task-download-1" / "inputs" / "cardiac_ultrasound" / "dcm-1.dcm"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_truncated_download_is_rejected_without_leaving_input_file(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        materializer = InputMaterializer(
            DownloadSettings(allowed_authorities=frozenset({authority}))
        )
        image = ImgItem(
            imgId="dcm-truncated",
            imgPath=f"http://{authority}/truncated.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )

        with pytest.raises(InputMaterializationError, match="下载不完整"):
            materializer.materialize(
                image,
                task_id="task-truncated",
                work_root=str(tmp_path),
            )

        input_dir = tmp_path / "task-truncated" / "inputs" / "cardiac_ultrasound"
        assert not (input_dir / "dcm-truncated.dcm").exists()
        assert not (input_dir / "dcm-truncated.dcm.part").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_input_is_rejected_when_host_is_not_allowlisted(tmp_path):
    materializer = InputMaterializer(DownloadSettings())
    image = ImgItem(
        imgId="dcm-denied",
        imgPath="https://files.example.invalid/studies/1.dcm",
        imgType="CARDIAC_ULTRASOUND",
        dcmType="PLAX",
    )

    with pytest.raises(InputMaterializationError, match="白名单"):
        materializer.materialize(image, task_id="task-denied", work_root=str(tmp_path))


def test_sanitized_input_ids_cannot_reuse_another_inputs_file(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        materializer = InputMaterializer(
            DownloadSettings(allowed_authorities=frozenset({authority}))
        )
        first = ImgItem(
            imgId="same/id",
            imgPath=f"http://{authority}/first.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )
        second = ImgItem(
            imgId="same?id",
            imgPath=f"http://{authority}/second.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )

        first_local = materializer.materialize(first, task_id="task-collision", work_root=str(tmp_path))
        second_local = materializer.materialize(second, task_id="task-collision", work_root=str(tmp_path))

        assert first_local.imgPath != second_local.imgPath
        assert Path(first_local.imgPath).read_bytes() == b"first-dicom"
        assert Path(second_local.imgPath).read_bytes() == b"second-dicom"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_echo_filesystem_failure_is_isolated_to_that_input(tmp_path):
    work_root_file = tmp_path / "not-a-directory"
    work_root_file.write_bytes(b"occupied")
    local_dicom = tmp_path / "local-good.dcm"
    local_dicom.write_bytes(_DownloadHandler.payload)
    echo_runner = _CapturingEchoRunner()
    runner = CombinedRunner(
        echo_runner=echo_runner,
        ecg_runner=_UnusedECGRunner(),
        input_materializer=InputMaterializer(
            DownloadSettings(allowed_authorities=frozenset({"files.example.invalid"}))
        ),
    )
    app = create_app(runner=runner, sync=True, work_root=str(work_root_file))

    with TestClient(app) as client:
        start = client.post("/heart-algo/task/start", json={
            "requestId": "request-filesystem-isolation",
            "sysUserId": "user-1",
            "taskId": "task-filesystem-isolation",
            "cardiacUltrasound": [{
                "dcmType": "PLAX",
                "dcms": [
                    {"dcmId": "dcm-local", "dcmPath": str(local_dicom)},
                    {
                        "dcmId": "dcm-filesystem-error",
                        "dcmPath": "https://files.example.invalid/studies/2.dcm",
                    },
                ],
            }],
            "ecg": [],
        })

    assert start.json()["taskState"] == 2


def test_configured_bearer_token_is_sent_to_download_service(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        _DownloadHandler.last_authorization = None
        materializer = InputMaterializer(DownloadSettings(
            allowed_authorities=frozenset({authority}),
            bearer_token="service-secret",
        ))
        image = ImgItem(
            imgId="dcm-auth",
            imgPath=f"http://{authority}/authenticated.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )

        materializer.materialize(image, task_id="task-auth", work_root=str(tmp_path))

        assert _DownloadHandler.last_authorization == "Bearer service-secret"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_existing_local_path_remains_compatible(tmp_path):
    source = tmp_path / "existing.dcm"
    source.write_bytes(b"local-dicom")
    image = ImgItem(
        imgId="dcm-local",
        imgPath=str(source),
        imgType="CARDIAC_ULTRASOUND",
        dcmType="PLAX",
    )

    resolved = InputMaterializer(DownloadSettings()).materialize(
        image,
        task_id="task-local",
        work_root=str(tmp_path),
    )

    assert resolved is image
    assert resolved.imgPath == str(source)


def test_oversized_download_is_rejected_without_partial_file(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        materializer = InputMaterializer(DownloadSettings(
            allowed_authorities=frozenset({authority}),
            max_bytes=4,
        ))
        image = ImgItem(
            imgId="dcm-large",
            imgPath=f"http://{authority}/large.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )

        with pytest.raises(InputMaterializationError, match="超过允许大小"):
            materializer.materialize(image, task_id="task-large", work_root=str(tmp_path))

        input_dir = tmp_path / "task-large" / "inputs" / "cardiac_ultrasound"
        assert not (input_dir / "dcm-large.dcm").exists()
        assert not (input_dir / "dcm-large.dcm.part").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_redirect_is_not_followed(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        materializer = InputMaterializer(
            DownloadSettings(allowed_authorities=frozenset({authority}))
        )
        image = ImgItem(
            imgId="dcm-redirect",
            imgPath=f"http://{authority}/redirect.dcm",
            imgType="CARDIAC_ULTRASOUND",
            dcmType="PLAX",
        )

        with pytest.raises(InputMaterializationError, match="不允许 HTTP 重定向"):
            materializer.materialize(image, task_id="task-redirect", work_root=str(tmp_path))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ecg_download_failure_marks_whole_task_failed(tmp_path):
    runner = CombinedRunner(
        echo_runner=_CapturingEchoRunner(),
        ecg_runner=_UnusedECGRunner(),
        input_materializer=InputMaterializer(DownloadSettings()),
    )
    app = create_app(runner=runner, sync=True, work_root=str(tmp_path))

    with TestClient(app) as client:
        start = client.post("/heart-algo/task/start", json={
            "requestId": "request-ecg-denied",
            "sysUserId": "user-1",
            "taskId": "task-ecg-denied",
            "cardiacUltrasound": [],
            "ecg": [{
                "ecgId": "ecg-denied",
                "ecgPath": "https://files.example.invalid/studies/1.xml",
            }],
        })

    assert start.json()["taskState"] == 3


def test_input_is_materialized_before_gpu_is_acquired(tmp_path):
    events: list[str] = []

    class RecordingMaterializer:
        def materialize(self, image, *, task_id, work_root):
            events.append("materialize")
            return image

    class RecordingPool:
        @contextmanager
        def acquire(self):
            events.append("acquire-gpu")
            yield "0"

    class RecordingEchoRunner:
        def run(self, imgs, task_id="", work_root=None, gpu_device=None):
            events.append("run-model")
            return {}

    image = ImgItem(
        imgId="dcm-order",
        imgPath=str(tmp_path / "existing.dcm"),
        imgType="CARDIAC_ULTRASOUND",
        dcmType="PLAX",
    )
    runner = CombinedRunner(
        echo_runner=RecordingEchoRunner(),
        ecg_runner=_UnusedECGRunner(),
        gpu_pool=RecordingPool(),
        input_materializer=RecordingMaterializer(),
    )

    runner.run([image], task_id="task-order", work_root=str(tmp_path))

    assert events == ["materialize", "acquire-gpu", "run-model"]


def test_one_echo_download_failure_does_not_discard_other_echo_results(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authority = f"127.0.0.1:{server.server_port}"
        echo_runner = _CapturingEchoRunner()
        runner = CombinedRunner(
            echo_runner=echo_runner,
            ecg_runner=_UnusedECGRunner(),
            input_materializer=InputMaterializer(
                DownloadSettings(allowed_authorities=frozenset({authority}))
            ),
        )
        app = create_app(runner=runner, sync=True, work_root=str(tmp_path))
        good_url = f"http://{authority}/ok.dcm"
        missing_url = f"http://{authority}/missing.dcm"

        with TestClient(app) as client:
            start = client.post("/heart-algo/task/start", json={
                "requestId": "request-download-isolation",
                "sysUserId": "user-1",
                "taskId": "task-download-isolation",
                "cardiacUltrasound": [{
                    "dcmType": "PLAX",
                    "dcms": [
                        {"dcmId": "dcm-ok", "dcmPath": good_url},
                        {"dcmId": "dcm-missing", "dcmPath": missing_url},
                    ],
                }],
                "ecg": [],
            })
            result = client.post("/heart-algo/task/result", json={
                "requestId": "result-download-isolation",
                "sysUserId": "user-1",
                "taskId": "task-download-isolation",
            }).json()

        assert start.json()["taskState"] == 2
        missing_report_id = next(
            item["reportId"]
            for item in result["cardiacUltrasound"]
            if item["dcmId"] == "dcm-missing"
        )
        missing_report = next(
            item for item in result["reports"] if item["reportId"] == missing_report_id
        )
        assert json.loads(missing_report["reportResult"])["error"] == "远程输入文件下载失败"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
