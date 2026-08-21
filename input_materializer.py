"""将远程算法输入下载到任务隔离目录，再交给只接受本地文件的 runner。

配置环境变量：

``HTTP_INPUT_ACCESS_POLICY``
    ``allowlist``（默认）要求初始地址匹配精确主机白名单；
    ``private_network`` 自动允许 RFC 1918 私有 IPv4 地址，无需主机白名单。
``HTTP_INPUT_ALLOWED_HOSTS``
    允许下载的精确 ``host`` 或 ``host:port``，多个值用英文逗号分隔。
    仅 ``allowlist`` 策略使用。默认空，即远程下载默认关闭；原有本地路径不受影响。
``HTTP_INPUT_MAX_BYTES``
    单个输入文件最大字节数，默认 512 MiB。
``HTTP_INPUT_TIMEOUT_SECONDS``
    单次 HTTP 请求 socket 超时秒数，默认 60 秒。
``HTTP_INPUT_BEARER_TOKEN``
    可选的服务间 Bearer Token，仅作为请求头发送，不写入文件路径或日志。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


class InputMaterializationError(RuntimeError):
    """输入引用无法安全物化为本地文件。"""


_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in _RFC1918_NETWORKS
    )


def _validate_connected_peer(connection: HTTPConnection | HTTPSConnection) -> None:
    """在发送 HTTP 请求头前校验实际 TCP 对端，防止 DNS 重绑定。"""
    try:
        peer = ipaddress.ip_address(connection.sock.getpeername()[0])
    except (AttributeError, OSError, ValueError) as exc:
        connection.close()
        raise InputMaterializationError("无法确认远程输入连接的实际对端") from exc
    if not _is_rfc1918(peer):
        connection.close()
        raise InputMaterializationError("远程输入实际连接不属于允许的私有网络")


class _PrivateNetworkHTTPConnection(HTTPConnection):
    def connect(self) -> None:
        super().connect()
        _validate_connected_peer(self)


class _PrivateNetworkHTTPSConnection(HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        _validate_connected_peer(self)


class _PrivateNetworkHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PrivateNetworkHTTPConnection, req)


class _PrivateNetworkHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _PrivateNetworkHTTPSConnection, req, context=self._context
        )


@dataclass(frozen=True)
class DownloadSettings:
    """HTTP 输入下载配置；默认使用精确白名单，也可允许私有网络。"""

    access_policy: str = "allowlist"
    allowed_authorities: frozenset[str] = frozenset()
    max_bytes: int = 512 * 1024 * 1024
    timeout_seconds: float = 60.0
    chunk_bytes: int = 1024 * 1024
    bearer_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.access_policy not in {"allowlist", "private_network"}:
            raise ValueError(
                "HTTP_INPUT_ACCESS_POLICY 必须是 allowlist 或 private_network"
            )
        if self.max_bytes < 1:
            raise ValueError("HTTP_INPUT_MAX_BYTES 必须大于 0")
        if self.timeout_seconds <= 0:
            raise ValueError("HTTP_INPUT_TIMEOUT_SECONDS 必须大于 0")
        if self.chunk_bytes < 1:
            raise ValueError("chunk_bytes 必须大于 0")
        if self.bearer_token and ("\r" in self.bearer_token or "\n" in self.bearer_token):
            raise ValueError("HTTP_INPUT_BEARER_TOKEN 不能包含换行符")

    @classmethod
    def from_environment(cls) -> "DownloadSettings":
        raw_hosts = os.environ.get("HTTP_INPUT_ALLOWED_HOSTS", "")
        hosts = frozenset(
            item.strip().lower().rstrip(".")
            for item in raw_hosts.split(",")
            if item.strip()
        )
        return cls(
            access_policy=os.environ.get("HTTP_INPUT_ACCESS_POLICY", "allowlist")
            .strip()
            .lower(),
            allowed_authorities=hosts,
            max_bytes=int(os.environ.get("HTTP_INPUT_MAX_BYTES", str(512 * 1024 * 1024))),
            timeout_seconds=float(os.environ.get("HTTP_INPUT_TIMEOUT_SECONDS", "60")),
            bearer_token=os.environ.get("HTTP_INPUT_BEARER_TOKEN") or None,
        )


class _RedirectHandler(HTTPRedirectHandler):
    """允许 HTTP(S) 重定向；跨来源跳转时不转发服务凭据。"""

    def __init__(self, validate_target: Callable[[str], None]) -> None:
        self._validate_target = validate_target

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validate_target(newurl)
        target = urlsplit(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source = urlsplit(req.full_url)
        if (source.scheme.lower(), source.netloc.lower()) != (
            target.scheme.lower(), target.netloc.lower()
        ):
            redirected.remove_header("Authorization")
        return redirected


class InputMaterializer:
    """支持本地路径直通，以及受控 HTTP/HTTPS 引用的任务级物化。"""

    def __init__(self, settings: DownloadSettings | None = None) -> None:
        self.settings = settings or DownloadSettings.from_environment()
        handlers = [_RedirectHandler(self._validate_redirect_url)]
        if self.settings.access_policy == "private_network":
            handlers.extend(
                [
                    ProxyHandler({}),
                    _PrivateNetworkHTTPHandler(),
                    _PrivateNetworkHTTPSHandler(),
                ]
            )
        self._opener = build_opener(*handlers)

    def materialize(self, image, *, task_id: str, work_root: str | None):
        try:
            parsed = urlsplit(image.imgPath)
        except ValueError as exc:
            raise InputMaterializationError("远程输入地址格式无效") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            return image
        if not task_id or not work_root:
            raise InputMaterializationError("远程输入下载要求配置任务隔离目录")

        try:
            self._validate_url(parsed, enforce_allowlist=True)
            destination = self._destination(image, task_id=task_id, work_root=work_root)
            if destination.is_file() and destination.stat().st_size > 0:
                return self._with_local_path(image, destination)

            self._download(image.imgPath, destination)
            return self._with_local_path(image, destination)
        except InputMaterializationError:
            raise
        except OSError as exc:
            raise InputMaterializationError("远程输入文件物化失败") from exc

    def _validate_redirect_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise InputMaterializationError("重定向地址格式无效") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise InputMaterializationError("重定向目标协议不受支持")
        self._validate_url(parsed, enforce_allowlist=False)

    def _validate_url(self, parsed: SplitResult, *, enforce_allowlist: bool) -> None:
        if parsed.username is not None or parsed.password is not None:
            raise InputMaterializationError("远程输入地址不允许包含用户凭据")
        try:
            port = parsed.port
        except ValueError as exc:
            raise InputMaterializationError("远程输入地址端口无效") from exc
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise InputMaterializationError("远程输入地址缺少主机名")
        authority = f"{host}:{port}" if port is not None else host
        if self.settings.access_policy == "private_network":
            self._validate_private_network_host(
                host, port or self._default_port(parsed.scheme)
            )
        elif enforce_allowlist and authority not in self.settings.allowed_authorities:
            raise InputMaterializationError("远程输入地址不在允许的下载白名单中")

    @staticmethod
    def _default_port(scheme: str) -> int:
        return 443 if scheme.lower() == "https" else 80

    @staticmethod
    def _validate_private_network_host(host: str, port: int) -> None:
        try:
            literal = ipaddress.ip_address(host)
            addresses = [literal]
        except ValueError:
            try:
                addresses = [
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                ]
            except (OSError, ValueError) as exc:
                raise InputMaterializationError("远程输入地址无法解析") from exc
        if not addresses or any(not _is_rfc1918(address) for address in addresses):
            raise InputMaterializationError("远程输入地址不属于允许的私有网络")

    def _destination(self, image, *, task_id: str, work_root: str) -> Path:
        safe_task = self._safe_component(task_id, "task")
        safe_image = self._safe_component(image.imgId, "input")
        if image.imgType == "CARDIAC_ULTRASOUND":
            category, suffix = "cardiac_ultrasound", ".dcm"
        elif image.imgType == "ECG":
            category, suffix = "ecg", ".xml"
        else:
            raise InputMaterializationError("输入文件类型不受支持")
        destination = Path(work_root) / safe_task / "inputs" / category / f"{safe_image}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    @staticmethod
    def _safe_component(value: str, fallback: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"{(safe or fallback)[:64]}-{digest}"

    @staticmethod
    def _with_local_path(image, destination: Path):
        data = image.model_dump() if hasattr(image, "model_dump") else image.dict()
        data["imgPath"] = str(destination)
        return type(image)(**data)

    def _download(self, source_url: str, destination: Path) -> None:
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        headers = {"User-Agent": "heart-algo-input-materializer/1"}
        if self.settings.bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.bearer_token}"
        request = Request(source_url, headers=headers)
        try:
            with self._opener.open(request, timeout=self.settings.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                expected_length = int(content_length) if content_length is not None else None
                if expected_length is not None and expected_length > self.settings.max_bytes:
                    raise InputMaterializationError("远程输入文件超过允许大小")
                total = 0
                with partial.open("xb") as output:
                    while chunk := response.read(self.settings.chunk_bytes):
                        total += len(chunk)
                        if total > self.settings.max_bytes:
                            raise InputMaterializationError("远程输入文件超过允许大小")
                        output.write(chunk)
                if total == 0:
                    raise InputMaterializationError("远程输入文件为空")
                if expected_length is not None and total != expected_length:
                    raise InputMaterializationError("远程输入文件下载不完整")
            os.replace(partial, destination)
        except InputMaterializationError:
            partial.unlink(missing_ok=True)
            raise
        except HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise InputMaterializationError("远程输入文件下载失败") from exc
        except (URLError, OSError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            raise InputMaterializationError("远程输入文件下载失败") from exc
