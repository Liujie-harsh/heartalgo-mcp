"""病例和上传资产的文件系统存储。

这里只保存算法闭环所需的技术元数据，不保存患者姓名等业务字段。病例目录和
资产文件均使用服务端生成或严格校验的标识，避免把上传文件名带入磁盘路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from xml.parsers import expat
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from uuid import uuid4

from metric_catalog import VIEW_METRICS


SUPPORTED_DCM_TYPES = frozenset(VIEW_METRICS)
SUPPORTED_MODALITIES = frozenset({"CARDIAC_ULTRASOUND", "ECG"})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class CaseStoreError(RuntimeError):
    """病例存储请求不合法或无法完成。"""


class CaseNotFoundError(CaseStoreError):
    """病例不存在，或对当前用户不可见。"""


class CaseConflictError(CaseStoreError):
    """同一标识已被用于不同内容。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value: str, field: str, max_length: int = 128) -> str:
    if len(value) > max_length or not _ID_PATTERN.fullmatch(value):
        raise CaseStoreError(f"{field} 格式无效")
    return value


class FileCaseStore:
    """以原子 JSON 元数据和不可变资产文件持久化病例。"""

    def __init__(self, root: str | Path, max_asset_bytes: int = 512 * 1024 * 1024) -> None:
        if max_asset_bytes < 1:
            raise ValueError("max_asset_bytes 必须大于 0")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_asset_bytes = max_asset_bytes
        self._lock = RLock()
        self._instance_lock_handle: BinaryIO | None = None

    @classmethod
    def from_environment(cls, root: str | Path) -> "FileCaseStore":
        return cls(
            root,
            max_asset_bytes=int(
                os.environ.get("CASE_ASSET_MAX_BYTES", str(512 * 1024 * 1024))
            ),
        )

    def acquire_instance_lock(self) -> None:
        """阻止两个生产进程同时修改同一病例目录。"""
        if self._instance_lock_handle is not None:
            return
        lock_path = self.root / ".instance.lock"
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CaseConflictError(
                "CASE_STORAGE_ROOT 已被另一个算法服务实例占用"
            ) from exc
        self._instance_lock_handle = handle

    def close_instance_lock(self) -> None:
        handle = self._instance_lock_handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._instance_lock_handle = None

    def create_case(
        self,
        sys_user_id: str,
        request_id: str,
        authorized_service_ids: list[str] | None = None,
    ) -> tuple[dict, bool]:
        _validate_id(sys_user_id, "sysUserId")
        _validate_id(request_id, "requestId")
        services = list(dict.fromkeys(authorized_service_ids or []))
        for service_id in services:
            _validate_id(service_id, "authorizedServiceId")
        with self._lock:
            for metadata_path in self.root.glob("case-*/case.json"):
                metadata = self._read_json(metadata_path)
                if (
                    metadata.get("sysUserId") == sys_user_id
                    and metadata.get("createRequestId") == request_id
                ):
                    existing_services = metadata.setdefault("authorizedServiceIds", [])
                    missing_services = [
                        item for item in services if item not in existing_services
                    ]
                    if missing_services:
                        existing_services.extend(missing_services)
                        self._write_case(metadata)
                    return metadata, False

            case_id = f"case-{uuid4().hex}"
            metadata = {
                "caseId": case_id,
                "sysUserId": sys_user_id,
                "createRequestId": request_id,
                "createdAt": _utc_now(),
                "assets": [],
                "diagnoses": [],
                "review": None,
                "reviewHistory": [],
                "authorizedServiceIds": services,
            }
            self._write_case(metadata)
            return metadata, True

    def get_case(self, case_id: str, sys_user_id: str) -> dict:
        _validate_id(case_id, "caseId")
        _validate_id(sys_user_id, "sysUserId")
        with self._lock:
            path = self._case_path(case_id)
            if not path.is_file():
                raise CaseNotFoundError("病例不存在")
            metadata = self._read_json(path)
            if metadata.get("sysUserId") != sys_user_id:
                raise CaseNotFoundError("病例不存在")
            return metadata

    def get_case_by_id(self, case_id: str) -> dict:
        """内部授权完成后读取病例；不得直接暴露为外部端点。"""
        _validate_id(case_id, "caseId")
        with self._lock:
            path = self._case_path(case_id)
            if not path.is_file():
                raise CaseNotFoundError("病例不存在")
            return self._read_json(path)

    def get_case_for_service(self, case_id: str, service_user_id: str) -> dict:
        """按病例级 ACL 授权 MCP 服务账号访问，不改变病例所有者。"""
        _validate_id(case_id, "caseId")
        _validate_id(service_user_id, "serviceUserId")
        with self._lock:
            path = self._case_path(case_id)
            if not path.is_file():
                raise CaseNotFoundError("病例不存在")
            metadata = self._read_json(path)
            if service_user_id not in metadata.get("authorizedServiceIds", []):
                raise CaseNotFoundError("病例不存在")
            return metadata

    def add_asset(
        self,
        case_id: str,
        sys_user_id: str,
        asset_id: str,
        modality: str,
        dcm_type: str | None,
        source: BinaryIO,
    ) -> tuple[dict, bool]:
        """流式保存一个不可变资产；同 ID 同内容重试视为幂等。"""
        _validate_id(asset_id, "assetId", max_length=64)
        if modality not in SUPPORTED_MODALITIES:
            raise CaseStoreError("modality 不受支持")
        if modality == "CARDIAC_ULTRASOUND":
            if dcm_type not in SUPPORTED_DCM_TYPES:
                raise CaseStoreError("dcmType 不受支持")
            suffix = ".dcm"
        else:
            if dcm_type not in (None, ""):
                raise CaseStoreError("ECG 资产不能设置 dcmType")
            dcm_type = None
            suffix = ".xml"

        self.get_case(case_id, sys_user_id)
        asset_dir = self.root / case_id / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        destination = asset_dir / f"{asset_id}{suffix}"
        partial = asset_dir / f".{asset_id}-{uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        head = bytearray()
        try:
            with partial.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_asset_bytes:
                        raise CaseStoreError("上传文件超过允许大小")
                    if len(head) < 512:
                        head.extend(chunk[: 512 - len(head)])
                    digest.update(chunk)
                    output.write(chunk)
            if total == 0:
                raise CaseStoreError("上传文件为空")
            self._validate_content(modality, bytes(head))
            if modality == "ECG":
                self._validate_ecg_xml_file(partial)

            asset = {
                "assetId": asset_id,
                "modality": modality,
                "dcmType": dcm_type,
                "sha256": digest.hexdigest(),
                "sizeBytes": total,
                "createdAt": _utc_now(),
                "path": str(destination),
            }
            with self._lock:
                metadata = self.get_case(case_id, sys_user_id)
                existing = next(
                    (item for item in metadata["assets"] if item["assetId"] == asset_id),
                    None,
                )
                if existing is not None:
                    if (
                        existing["sha256"] == asset["sha256"]
                        and existing["modality"] == modality
                        and existing.get("dcmType") == dcm_type
                    ):
                        return existing, False
                    raise CaseConflictError("assetId 已用于其他文件")
                os.replace(partial, destination)
                metadata["assets"].append(asset)
                self._write_case(metadata)
                return asset, True
        finally:
            partial.unlink(missing_ok=True)

    def reserve_diagnosis(
        self,
        case_id: str,
        sys_user_id: str,
        task_id: str,
        request_id: str,
        asset_ids: list[str],
    ) -> tuple[dict, bool]:
        """先持久化提交意图；相同请求只有输入完全相同时才视为幂等。"""
        with self._lock:
            metadata = self.get_case(case_id, sys_user_id)
            existing = next(
                (
                    item
                    for item in metadata["diagnoses"]
                    if item.get("requestId") == request_id
                ),
                None,
            )
            if existing is not None:
                if existing.get("assetIds") != list(asset_ids):
                    raise CaseConflictError("requestId 已用于不同的诊断输入")
                return existing, False
            diagnosis = {
                "taskId": task_id,
                "requestId": request_id,
                "assetIds": list(asset_ids),
                "submissionState": "reserved",
                "createdAt": _utc_now(),
            }
            metadata["diagnoses"].append(diagnosis)
            self._write_case(metadata)
            return diagnosis, True

    def mark_diagnosis_submitted(
        self, case_id: str, sys_user_id: str, task_id: str
    ) -> None:
        with self._lock:
            metadata = self.get_case(case_id, sys_user_id)
            diagnosis = next(
                (item for item in metadata["diagnoses"] if item["taskId"] == task_id),
                None,
            )
            if diagnosis is None:
                raise CaseNotFoundError("诊断任务不存在")
            diagnosis["submissionState"] = "submitted"
            self._write_case(metadata)

    def reserved_diagnoses(self) -> list[tuple[dict, dict]]:
        """返回需要在启动时补建任务的提交意图。"""
        with self._lock:
            pending: list[tuple[dict, dict]] = []
            for metadata_path in self.root.glob("case-*/case.json"):
                metadata = self._read_json(metadata_path)
                for diagnosis in metadata.get("diagnoses", []):
                    if diagnosis.get("submissionState") == "reserved":
                        pending.append((metadata, diagnosis))
            return pending

    def find_case_for_task(self, task_id: str, sys_user_id: str) -> str:
        """为只持有 taskId 的 MCP 资源定位服务账号下的病例。"""
        with self._lock:
            for metadata_path in self.root.glob("case-*/case.json"):
                metadata = self._read_json(metadata_path)
                if sys_user_id not in metadata.get("authorizedServiceIds", []):
                    continue
                if any(item.get("taskId") == task_id for item in metadata.get("diagnoses", [])):
                    return metadata["caseId"]
        raise CaseNotFoundError("诊断任务不存在")

    def list_cases_for_service(self, service_user_id: str) -> list[dict]:
        """列出授权给服务账号的病例摘要（不含磁盘路径）。"""
        _validate_id(service_user_id, "serviceUserId")
        summaries: list[dict] = []
        with self._lock:
            for metadata_path in sorted(self.root.glob("case-*/case.json")):
                try:
                    metadata = self._read_json(metadata_path)
                except CaseStoreError:
                    continue
                if service_user_id not in metadata.get("authorizedServiceIds", []):
                    continue
                review = metadata.get("review") or {}
                summaries.append({
                    "caseId": metadata["caseId"],
                    "sysUserId": metadata.get("sysUserId"),
                    "createdAt": metadata.get("createdAt"),
                    "assetCount": len(metadata.get("assets", [])),
                    "diagnosisCount": len(metadata.get("diagnoses", [])),
                    "reviewDecision": review.get("decision"),
                })
        return summaries

    def diagnosis_belongs_to_case(
        self, case_id: str, sys_user_id: str, task_id: str
    ) -> bool:
        metadata = self.get_case(case_id, sys_user_id)
        return any(item["taskId"] == task_id for item in metadata["diagnoses"])

    def record_review(
        self,
        case_id: str,
        sys_user_id: str,
        task_id: str,
        reviewer_id: str,
        decision: str,
        comment: str,
    ) -> dict:
        """追加临床复核审计记录，并更新病例的当前复核状态。"""
        _validate_id(reviewer_id, "reviewerId")
        if decision not in {"approved", "rejected"}:
            raise CaseStoreError("decision 必须是 approved 或 rejected")
        if len(comment) > 2000:
            raise CaseStoreError("comment 不能超过 2000 个字符")
        with self._lock:
            metadata = self.get_case(case_id, sys_user_id)
            if not any(item["taskId"] == task_id for item in metadata["diagnoses"]):
                raise CaseNotFoundError("诊断任务不存在")
            review = {
                "taskId": task_id,
                "reviewerId": reviewer_id,
                "decision": decision,
                "comment": comment,
                "reviewedAt": _utc_now(),
            }
            metadata.setdefault("reviewHistory", []).append(review)
            metadata["review"] = review
            self._write_case(metadata)
            return review

    @staticmethod
    def public_case(metadata: dict) -> dict:
        """移除磁盘路径和内部幂等字段。"""
        return {
            "caseId": metadata["caseId"],
            "createdAt": metadata["createdAt"],
            "assets": [
                {key: value for key, value in asset.items() if key != "path"}
                for asset in metadata["assets"]
            ],
            "diagnoses": list(metadata["diagnoses"]),
            "artifacts": [
                {key: value for key, value in artifact.items() if key != "path"}
                for artifact in metadata.get("artifacts", [])
            ],
            "review": metadata.get("review"),
            "reviewHistory": list(metadata.get("reviewHistory", [])),
        }

    def save_case_artifact(
        self,
        case_id: str,
        sys_user_id: str,
        artifact_id: str,
        content: str | bytes,
    ) -> dict:
        """把报告等工件原子写入病例目录并登记元数据；同 ID 同内容幂等。"""
        _validate_id(case_id, "caseId")
        _validate_id(sys_user_id, "sysUserId")
        _validate_id(artifact_id, "artifactId")
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if not data:
            raise CaseStoreError("工件内容为空")
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            metadata = self.get_case(case_id, sys_user_id)
            artifacts = metadata.setdefault("artifacts", [])
            existing = next(
                (item for item in artifacts if item["artifactId"] == artifact_id),
                None,
            )
            if existing is not None:
                if existing["sha256"] == digest:
                    return existing
                raise CaseConflictError("artifactId 已用于其他内容")
            directory = self.root / case_id / "artifacts"
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / artifact_id
            partial = directory / f".{artifact_id}-{uuid4().hex}.part"
            try:
                partial.write_bytes(data)
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
            artifact = {
                "artifactId": artifact_id,
                "sha256": digest,
                "sizeBytes": len(data),
                "createdAt": _utc_now(),
                "path": str(destination),
            }
            artifacts.append(artifact)
            self._write_case(metadata)
            return artifact

    def read_case_artifact(
        self, case_id: str, sys_user_id: str, artifact_id: str
    ) -> tuple[str, dict]:
        """读取病例工件文本内容及其元数据（返回值不含磁盘路径）。"""
        _validate_id(case_id, "caseId")
        _validate_id(sys_user_id, "sysUserId")
        _validate_id(artifact_id, "artifactId")
        metadata = self.get_case(case_id, sys_user_id)
        artifact = next(
            (
                item
                for item in metadata.get("artifacts", [])
                if item["artifactId"] == artifact_id
            ),
            None,
        )
        if artifact is None:
            raise CaseNotFoundError("工件不存在")
        path = self.root / case_id / "artifacts" / artifact_id
        if not path.is_file():
            raise CaseNotFoundError("工件文件缺失")
        try:
            return path.read_text(encoding="utf-8"), artifact
        except OSError as exc:
            raise CaseStoreError("工件读取失败") from exc

    @staticmethod
    def _validate_content(modality: str, head: bytes) -> None:
        if modality == "CARDIAC_ULTRASOUND":
            if len(head) < 132 or head[128:132] != b"DICM":
                raise CaseStoreError("心超文件不是受支持的 DICOM Part 10 文件")
        else:
            xml_head = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
            if not xml_head.lstrip().startswith(b"<"):
                raise CaseStoreError("ECG 文件不是 XML")

    @staticmethod
    def _validate_ecg_xml_file(path: Path) -> None:
        """流式验证完整 XML，避免只凭文件头接受截断或伪造内容。"""
        parser = expat.ParserCreate()
        root_name: str | None = None

        def reject_declaration(*_args) -> None:
            raise CaseStoreError("ECG XML 不允许 DTD 或实体声明")

        def capture_root(name: str, _attributes) -> None:
            nonlocal root_name
            if root_name is None:
                root_name = name.rsplit(":", 1)[-1]

        parser.StartElementHandler = capture_root
        parser.StartDoctypeDeclHandler = reject_declaration
        parser.EntityDeclHandler = reject_declaration
        parser.UnparsedEntityDeclHandler = reject_declaration
        parser.ExternalEntityRefHandler = reject_declaration
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        try:
            with path.open("rb") as source:
                parser.ParseFile(source)
        except (OSError, expat.ExpatError) as exc:
            raise CaseStoreError("ECG XML 格式无效") from exc
        if root_name not in {"AnnotatedECG", "ECG"}:
            raise CaseStoreError("ECG XML 根元素不受支持")

    def _case_path(self, case_id: str) -> Path:
        return self.root / case_id / "case.json"

    def _write_case(self, metadata: dict) -> None:
        path = self._case_path(metadata["caseId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".json.{uuid4().hex}.part")
        try:
            partial.write_text(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CaseStoreError("病例元数据损坏") from exc
