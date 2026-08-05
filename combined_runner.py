"""按 imgType 分流 EchoNet (心超) / ECG-FM (心电图) 的组合 runner。"""
from __future__ import annotations

from api import ImgItem


class CombinedRunner:
    """Cardiac Ultrasound → EchoNetRunner, ECG → ECGFMRunner, 合并结果。

    task_id / work_root 透传给子 runner, 做任务目录隔离 (handoff L517-L552)。
    """

    def __init__(self, echo_runner, ecg_runner):
        self.echo_runner = echo_runner
        self.ecg_runner = ecg_runner

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        result: dict = {}
        echo_images = [image for image in imgs if image.imgType == "Cardiac Ultrasound"]
        ecg_images = [image for image in imgs if image.imgType == "ECG"]
        if echo_images:
            result.update(self.echo_runner.run(echo_images, task_id=task_id, work_root=work_root))
        if ecg_images:
            result.update(self.ecg_runner.run(ecg_images, task_id=task_id, work_root=work_root))
        return result
