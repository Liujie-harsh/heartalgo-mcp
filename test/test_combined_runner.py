"""CombinedRunner 测试: 心超/ECG 分流 + GPU 资源池 + 结果合并。

v3 变更:
  - imgType 枚举大写: CARDIAC_ULTRASOUND / ECG (与 DB input_type 对齐)
  - ECG runner 返回 ecg_predictions + ecg_measurements + ecg_patient_info
  - CombinedRunner 支持 GPUResourcePool (并行调度, 按空闲 GPU 分配)
"""
from api import ImgItem
from combined_runner import CombinedRunner, GPUResourcePool, InProcessTaskQueue


class EchoStub:
    def __init__(self):
        self.calls = []

    def run(self, imgs, task_id="", work_root=None, gpu_device=None):
        self.calls.append((imgs, gpu_device))
        return {
            "lvef": 55.0, "lvedd": 50.0, "lvesd": 32.0, "lad": 34.0, "mv_ea": 1.1,
            "echo_per_image": {"echo-1": {"lvef": 55.0, "lvedd": 50.0}},
        }


class EcgStub:
    def __init__(self):
        self.calls = []

    def run(self, imgs, task_id="", work_root=None, gpu_device=None):
        self.calls.append((imgs, gpu_device))
        return {
            "ecg_predictions": {"ecg-1": [{"label": "窦性心律", "probability": 0.99}]},
            "ecg_measurements": {"ecg-1": {"ventRate": 63}},
            "ecg_patient_info": {"ecg-1": {"patientId": "P001", "age": 72, "sex": "M"}},
        }


# ────────────────── 分流 + 结果合并 ──────────────────

def test_combined_runner_routes_by_img_type():
    # v3: imgType 用大写枚举 CARDIAC_ULTRASOUND / ECG
    echo = EchoStub()
    ecg = EcgStub()
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg)
    imgs = [
        ImgItem(imgId="echo-1", imgPath="echo.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="PLAX"),
        ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG"),
    ]

    result = runner.run(imgs)

    assert echo.calls[0][0] == [imgs[0]]
    assert ecg.calls[0][0] == [imgs[1]]
    assert result["lvef"] == 55.0
    assert result["ecg_predictions"]["ecg-1"][0]["label"] == "窦性心律"
    assert result["ecg_measurements"]["ecg-1"]["ventRate"] == 63
    assert result["ecg_patient_info"]["ecg-1"]["age"] == 72


def test_combined_runner_echo_only():
    # 仅心超: ECG runner 不被调用
    echo = EchoStub()
    ecg = EcgStub()
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg)
    imgs = [ImgItem(imgId="echo-1", imgPath="echo.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="A4C")]

    result = runner.run(imgs)

    assert len(echo.calls) == 1
    assert len(ecg.calls) == 0
    assert result["lvef"] == 55.0


def test_combined_runner_ecg_only():
    # 仅 ECG: 心超 runner 不被调用
    echo = EchoStub()
    ecg = EcgStub()
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg)
    imgs = [ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG")]

    result = runner.run(imgs)

    assert len(echo.calls) == 0
    assert len(ecg.calls) == 1
    assert result["ecg_predictions"]["ecg-1"][0]["probability"] == 0.99


def test_combined_runner_empty_imgs():
    # 空列表: 两个 runner 都不调用, 返回空 dict
    echo = EchoStub()
    ecg = EcgStub()
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg)

    result = runner.run([])

    assert result == {}
    assert len(echo.calls) == 0
    assert len(ecg.calls) == 0


# ────────────────── GPU 资源池 ──────────────────

def test_gpu_pool_parallel_dispatch():
    # 有 GPU 池时, 心超+ECG 并行执行, 各占一张卡
    echo = EchoStub()
    ecg = EcgStub()
    pool = GPUResourcePool(gpu_ids=["0", "1"])
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg, gpu_pool=pool)
    imgs = [
        ImgItem(imgId="echo-1", imgPath="echo.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="PLAX"),
        ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG"),
    ]

    runner.run(imgs)

    # 两 runner 各被调用一次, 且分配到不同 GPU
    echo_gpu = echo.calls[0][1]
    ecg_gpu = ecg.calls[0][1]
    assert echo_gpu is not None
    assert ecg_gpu is not None
    assert echo_gpu != ecg_gpu


def test_gpu_pool_single_card_serial():
    # 单卡 GPU 池: 心超和 ECG 串行使用同一张卡
    echo = EchoStub()
    ecg = EcgStub()
    pool = GPUResourcePool(gpu_ids=["0"])
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg, gpu_pool=pool)
    imgs = [
        ImgItem(imgId="echo-1", imgPath="echo.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="PLAX"),
        ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG"),
    ]

    runner.run(imgs)

    assert echo.calls[0][1] == "0"
    assert ecg.calls[0][1] == "0"


def test_gpu_pool_rejects_duplicate_ids():
    # GPU 池拒绝重复 ID
    try:
        GPUResourcePool(gpu_ids=["0", "0"])
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_gpu_pool_rejects_empty():
    # GPU 池拒绝空列表
    try:
        GPUResourcePool(gpu_ids=[])
        assert False, "应抛 ValueError"
    except ValueError:
        pass


# ────────────────── 进程内任务队列 ──────────────────

def test_task_queue_executes_async():
    # 队列异步执行任务
    queue = InProcessTaskQueue(worker_count=1)
    results = []
    queue.enqueue(lambda: results.append("done"))
    queue.join()
    queue.close()
    assert results == ["done"]


def test_task_queue_worker_count_validation():
    # worker_count 必须 >= 1
    try:
        InProcessTaskQueue(worker_count=0)
        assert False, "应抛 ValueError"
    except ValueError:
        pass
