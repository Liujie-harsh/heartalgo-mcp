"""病例和上传资产的文件系统存储。

这里只保存算法闭环所需的技术元数据，不保存患者姓名等业务字段。病例目录和
资产文件均使用服务端生成或严格校验的标识，避免把上传文件名带入磁盘路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from uuid import uuid4


SUPPORTED_DCM_TYPES = frozenset({
    "PLAX", "A4C", "Subcostal", "RVOT", "MV_EA", "AV_Vmax",
    "TR_Vmax", "MR_Vmax", "LVOT_Vmax", "TDI_Medial",
    "TDI_Lateral", "TAPSE",
})
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

    @classmethod
    def from_environment(cls, root: str | Path) -> "FileCaseStore":
        return cls(
            root,
            max_asset_bytes=int(
                os.environ.get("CASE_ASSET_MAX_BYTES", str(512 * 1024 * 1024))
            ),
        )

    def create_case(self, sys_user_id: str, request_id: str) -> tuple[dict, bool]:
        _validate_id(sys_user_id, "sysUserId")
        _validate_id(request_id, "requestId")
        with self._lock:
            for metadata_path in self.root.glob("case-*/case.json"):
                metadata = self._read_json(metadata_path)
                if (
                    metadata.get("sysUserId") == sys_user_id
                    and metadata.get("createRequestId") == request_id
                ):
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

    def record_diagnosis(
        self,
        case_id: str,
        sys_user_id: str,
        task_id: str,
        request_id: str,
        asset_ids: list[str],
    ) -> None:
        with self._lock:
            metadata = self.get_case(case_id, sys_user_id)
            existing = next(
                (item for item in metadata["diagnoses"] if item["taskId"] == task_id),
                None,
            )
            if existing is None:
                metadata["diagnoses"].append({
                    "taskId": task_id,
                    "requestId": request_id,
                    "assetIds": list(asset_ids),
                    "createdAt": _utc_now(),
                })
                self._write_case(metadata)

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
            "review": metadata.get("review"),
            "reviewHistory": list(metadata.get("reviewHistory", [])),
        }

    @staticmethod
    def _validate_content(modality: str, head: bytes) -> None:
        if modality == "CARDIAC_ULTRASOUND":
            if len(head) < 132 or head[128:132] != b"DICM":
                raise CaseStoreError("心超文件不是受支持的 DICOM Part 10 文件")
        elif not head.lstrip().startswith(b"<"):
            raise CaseStoreError("ECG 文件不是 XML")

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
