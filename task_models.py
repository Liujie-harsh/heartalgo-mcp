"""任务执行层共享的数据模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ImgItem(BaseModel):
    """由接口请求展平后的单个心超或 ECG 输入。"""

    imgId: str
    imgPath: str
    imgType: str
    dcmType: Optional[str] = None
