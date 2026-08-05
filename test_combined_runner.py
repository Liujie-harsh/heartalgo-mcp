from api import ImgItem
from combined_runner import CombinedRunner


class EchoStub:
    def __init__(self):
        self.calls = []

    def run(self, imgs, task_id="", work_root=None):
        self.calls.append(imgs)
        return {"lvef": 55.0, "lvedd": 50.0, "lvesd": 32.0, "lad": 34.0, "ea": 1.1, "gls": None}


class EcgStub:
    def __init__(self):
        self.calls = []

    def run(self, imgs, task_id="", work_root=None):
        self.calls.append(imgs)
        return {"ecg_predictions": {"ecg-1": [{"label": "Sinus rhythm", "probability": 0.99}]}}


def test_combined_runner_routes_each_modality_to_its_own_runner():
    echo = EchoStub()
    ecg = EcgStub()
    runner = CombinedRunner(echo_runner=echo, ecg_runner=ecg)
    imgs = [
        ImgItem(imgId="echo-1", imgPath="echo.dcm", imgType="Cardiac Ultrasound"),
        ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG"),
    ]

    result = runner.run(imgs)

    assert echo.calls == [[imgs[0]]]
    assert ecg.calls == [[imgs[1]]]
    assert result["lvef"] == 55.0
    assert result["ecg_predictions"]["ecg-1"][0]["label"] == "Sinus rhythm"
