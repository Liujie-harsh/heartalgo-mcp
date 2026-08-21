"""算法服务对外错误契约。

Runner 可以抛出 ``AlgorithmError`` 的具体子类表达可安全公开的错误；
其他异常一律转换为通用内部错误，原始异常仅由调用方写入受控日志。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicError:
    code: str
    message: str
    retryable: bool = False


class AlgorithmError(Exception):
    """携带稳定错误码与可公开中文消息的业务异常。"""

    code = "ALGORITHM_ERROR"
    default_message = "算法处理失败"
    retryable = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)

    @property
    def public_error(self) -> PublicError:
        return PublicError(self.code, str(self), self.retryable)


def to_public_error(error: Exception) -> PublicError:
    """将任意异常规范化为不会泄露内部细节的对外错误。"""
    if isinstance(error, AlgorithmError):
        return error.public_error
    return PublicError(
        code="INTERNAL_ERROR",
        message="算法服务内部错误，请联系管理员",
        retryable=False,
    )
