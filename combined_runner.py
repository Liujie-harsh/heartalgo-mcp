"""Python 内部任务队列、多 GPU 资源池及心超/ECG 组合推理器。"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING, Any, Iterator

from input_materializer import InputMaterializationError, InputMaterializer

if TYPE_CHECKING:
    from api import ImgItem


class InProcessTaskQueue:
    """进程内后台任务队列；默认单 Worker，避免未配置多 GPU 时显存竞争。"""
    def __init__(self, worker_count: int = 1, max_size: int = 0) -> None:
        if worker_count < 1 or max_size < 0:
            raise ValueError("worker_count 必须大于 0，max_size 不能小于 0")
        self._queue: Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]] | None] = Queue(maxsize=max_size)
        self._closed = False
        self._workers = [Thread(target=self._work, name=f"heart-algo-worker-{i}", daemon=True) for i in range(worker_count)]
        for worker in self._workers:
            worker.start()

    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self._closed:
            raise RuntimeError("任务队列已关闭，不能继续提交任务")
        self._queue.put((func, args, kwargs))

    def join(self) -> None:
        self._queue.join()

    def close(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        for _ in self._workers:
            self._queue.put(None)
        if wait:
            for worker in self._workers:
                worker.join()

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                func, args, kwargs = item
                try:
                    func(*args, **kwargs)
                except Exception:
                    # 业务异常由 API 写入任务失败状态，不能让 Worker 停止。
                    pass
            finally:
                self._queue.task_done()


class GPUResourcePool:
    """线程安全 GPU 资源池；同一时刻每张卡只能被一个模型子任务占用。"""
    def __init__(self, gpu_ids: list[str] | tuple[str, ...]) -> None:
        ids = [str(item).strip() for item in gpu_ids if str(item).strip()]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("至少配置一个且不重复的 GPU 编号")
        self._available: Queue[str] = Queue()
        for gpu_id in ids:
            self._available.put(gpu_id)

    @contextmanager
    def acquire(self) -> Iterator[str]:
        gpu_id = self._available.get()
        try:
            yield gpu_id
        finally:
            self._available.put(gpu_id)


class CombinedRunner:
    """心超与 ECG 任务可占用不同空闲 GPU；全部完成后合并为一个 work 的结果。"""
    def __init__(
        self,
        echo_runner,
        ecg_runner,
        gpu_pool: GPUResourcePool | None = None,
        input_materializer: InputMaterializer | None = None,
    ):
        self.echo_runner = echo_runner
        self.ecg_runner = ecg_runner
        self.gpu_pool = gpu_pool
        self.input_materializer = input_materializer or InputMaterializer()

    @staticmethod
    def _call(runner, imgs: list[ImgItem], task_id: str, work_root: str | None, gpu_id: str | None) -> dict:
        kwargs = {"task_id": task_id, "work_root": work_root}
        if gpu_id is not None and "gpu_device" in inspect.signature(runner.run).parameters:
            kwargs["gpu_device"] = gpu_id
        return runner.run(imgs, **kwargs)

    def _run_with_gpu(self, runner, imgs, task_id, work_root) -> dict:
        if self.gpu_pool is None:
            return self._call(runner, imgs, task_id, work_root, None)
        with self.gpu_pool.acquire() as gpu_id:
            return self._call(runner, imgs, task_id, work_root, gpu_id)

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        materialized = []
        echo_download_errors: dict[str, dict] = {}
        for image in imgs:
            try:
                materialized.append(
                    self.input_materializer.materialize(image, task_id=task_id, work_root=work_root)
                )
            except InputMaterializationError as exc:
                if image.imgType == "ECG":
                    raise
                echo_download_errors[image.imgId] = {"error": str(exc), "rois": []}
        echo_images = [image for image in materialized if image.imgType == "CARDIAC_ULTRASOUND"]
        ecg_images = [image for image in materialized if image.imgType == "ECG"]
        if echo_images and ecg_images and self.gpu_pool is not None:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="heart-algo-model") as executor:
                echo = executor.submit(self._run_with_gpu, self.echo_runner, echo_images, task_id, work_root)
                ecg = executor.submit(self._run_with_gpu, self.ecg_runner, ecg_images, task_id, work_root)
                result = {**echo.result(), **ecg.result()}
                return self._with_download_errors(result, echo_download_errors)
        result: dict = {}
        if echo_images:
            result.update(self._run_with_gpu(self.echo_runner, echo_images, task_id, work_root))
        if ecg_images:
            result.update(self._run_with_gpu(self.ecg_runner, ecg_images, task_id, work_root))
        return self._with_download_errors(result, echo_download_errors)

    @staticmethod
    def _with_download_errors(result: dict, errors: dict[str, dict]) -> dict:
        if errors:
            result.setdefault("echo_per_image", {}).update(errors)
        return result
