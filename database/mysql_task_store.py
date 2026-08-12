"""基于 MySQL 8 的任务持久化实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from task_models import ImgItem
from task_store import TaskOwnershipError


class MySQLTaskStore:
    """使用 algorithm_task/input/report 三张表实现 TaskStore 契约。

    开发阶段按既定策略不写 algorithm_execution；报告的 execution_id 为 NULL。
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None and not database_url:
            raise ValueError("database_url or engine is required")
        self._owns_engine = engine is None
        self._engine = engine or create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()

    def create_or_get(
        self,
        task_id: str,
        imgs: list[ImgItem],
        request_id: str = "",
        sys_user_id: str = "",
    ) -> tuple[str, dict, bool]:
        existing = self._find_existing(task_id, request_id, sys_user_id)
        if existing is not None:
            return existing["taskId"], existing["task"], False

        try:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO algorithm_task
                            (task_id, request_id, sys_user_id, task_state)
                        VALUES
                            (:task_id, :request_id, :sys_user_id, 0)
                        """
                    ),
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "sys_user_id": sys_user_id,
                    },
                )
                task_db_id = connection.execute(
                    text("SELECT id FROM algorithm_task WHERE task_id = :task_id"),
                    {"task_id": task_id},
                ).scalar_one()
                if imgs:
                    connection.execute(
                        text(
                            """
                            INSERT INTO algorithm_input
                                (task_db_id, input_type, input_id, input_path, dcm_type, input_state)
                            VALUES
                                (:task_db_id, :input_type, :input_id, :input_path, :dcm_type, 0)
                            """
                        ),
                        [
                            {
                                "task_db_id": task_db_id,
                                "input_type": image.imgType,
                                "input_id": image.imgId,
                                "input_path": image.imgPath,
                                "dcm_type": image.dcmType,
                            }
                            for image in imgs
                        ],
                    )
        except IntegrityError:
            # 唯一键承担并发幂等仲裁；冲突事务回滚后重新读取胜出的任务。
            existing = self._find_existing(task_id, request_id, sys_user_id)
            if existing is None:
                raise
            return existing["taskId"], existing["task"], False

        task = self.get(task_id)
        if task is None:  # pragma: no cover - 仅防御异常数据库行为
            raise RuntimeError(f"created task cannot be loaded: {task_id}")
        return task_id, task, True

    def _find_existing(self, task_id: str, request_id: str, sys_user_id: str) -> Optional[dict]:
        if request_id and sys_user_id:
            with self._engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT task_id
                        FROM algorithm_task
                        WHERE sys_user_id = :sys_user_id AND request_id = :request_id
                        """
                    ),
                    {"sys_user_id": sys_user_id, "request_id": request_id},
                ).mappings().first()
            if row is not None:
                task = self.get(row["task_id"])
                return {"taskId": row["task_id"], "task": task}

        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT sys_user_id FROM algorithm_task WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).mappings().first()
        if row is None:
            return None
        if row["sys_user_id"] != sys_user_id:
            raise TaskOwnershipError("task id conflict")
        task = self.get(task_id)
        return {"taskId": task_id, "task": task}

    def get(self, task_id: str) -> Optional[dict]:
        with self._engine.connect() as connection:
            return self._load_task(connection, task_id)

    def get_for_user(self, task_id: str, sys_user_id: str) -> Optional[dict]:
        with self._engine.connect() as connection:
            return self._load_task(connection, task_id, sys_user_id=sys_user_id)

    def claim(self, task_id: str) -> bool:
        with self._engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE algorithm_task
                    SET task_state = 1, started_at = CURRENT_TIMESTAMP
                    WHERE task_id = :task_id AND task_state = 0
                    """
                ),
                {"task_id": task_id},
            )
            if updated.rowcount != 1:
                return False
            connection.execute(
                text(
                    """
                    UPDATE algorithm_input
                    SET input_state = 1
                    WHERE task_db_id = (
                        SELECT id FROM algorithm_task WHERE task_id = :task_id
                    ) AND input_state = 0
                    """
                ),
                {"task_id": task_id},
            )
            return True

    def complete(self, task_id: str, result: dict) -> bool:
        with self._engine.begin() as connection:
            task_row = connection.execute(
                text("SELECT id, task_state FROM algorithm_task WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).mappings().first()
            if task_row is None or task_row["task_state"] != 1:
                return False

            input_rows = connection.execute(
                text(
                    """
                    SELECT id, input_type, input_id
                    FROM algorithm_input
                    WHERE task_db_id = :task_db_id
                    """
                ),
                {"task_db_id": task_row["id"]},
            ).mappings().all()
            input_ids = {
                (row["input_type"], row["input_id"]): row["id"]
                for row in input_rows
            }

            for report in result.get("reports", []):
                report_type = report["reportType"]
                input_type = {
                    "CU-SUB": "CARDIAC_ULTRASOUND",
                    "ECG": "ECG",
                }.get(report_type)
                input_db_id = (
                    input_ids.get((input_type, report.get("inputId")))
                    if input_type is not None
                    else None
                )
                if input_type is not None and input_db_id is None:
                    raise ValueError(
                        f"report input not found: {report_type}/{report.get('inputId')}"
                    )
                connection.execute(
                    text(
                        """
                        INSERT INTO algorithm_report
                            (task_db_id, input_db_id, execution_id, report_id,
                             report_type, report_result, roi_data)
                        VALUES
                            (:task_db_id, :input_db_id, NULL, :report_id,
                             :report_type, :report_result, :roi_data)
                        """
                    ),
                    {
                        "task_db_id": task_row["id"],
                        "input_db_id": input_db_id,
                        "report_id": report["reportId"],
                        "report_type": report_type,
                        "report_result": self._dump_json(report["reportResult"]),
                        "roi_data": self._dump_json(report.get("roiData")),
                    },
                )
                if input_db_id is not None:
                    payload = report["reportResult"]
                    reason = payload.get("error") or payload.get("skipReason")
                    connection.execute(
                        text(
                            """
                            UPDATE algorithm_input
                            SET input_state = :input_state, failed_reason = :failed_reason
                            WHERE id = :input_db_id
                            """
                        ),
                        {
                            "input_state": 3 if reason else 2,
                            "failed_reason": reason,
                            "input_db_id": input_db_id,
                        },
                    )

            updated = connection.execute(
                text(
                    """
                    UPDATE algorithm_task
                    SET task_state = 2, failed_reason = NULL,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE id = :task_db_id AND task_state = 1
                    """
                ),
                {"task_db_id": task_row["id"]},
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"task state changed while completing: {task_id}")
            return True

    def fail(self, task_id: str, reason: str) -> bool:
        with self._engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE algorithm_task
                    SET task_state = 3, failed_reason = :reason,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE task_id = :task_id AND task_state IN (0, 1)
                    """
                ),
                {"task_id": task_id, "reason": reason},
            )
            if updated.rowcount != 1:
                return False
            connection.execute(
                text(
                    """
                    UPDATE algorithm_input
                    SET input_state = 3, failed_reason = :reason
                    WHERE task_db_id = (
                        SELECT id FROM algorithm_task WHERE task_id = :task_id
                    ) AND input_state IN (0, 1)
                    """
                ),
                {"task_id": task_id, "reason": reason},
            )
            return True

    def _load_task(
        self,
        connection,
        task_id: str,
        *,
        sys_user_id: str | None = None,
    ) -> Optional[dict]:
        sql = """
            SELECT id, task_id, request_id, sys_user_id, task_state, failed_reason
            FROM algorithm_task
            WHERE task_id = :task_id
        """
        params = {"task_id": task_id}
        if sys_user_id is not None:
            sql += " AND sys_user_id = :sys_user_id"
            params["sys_user_id"] = sys_user_id
        task_row = connection.execute(text(sql), params).mappings().first()
        if task_row is None:
            return None

        input_rows = connection.execute(
            text(
                """
                SELECT id, input_type, input_id, input_path, dcm_type
                FROM algorithm_input
                WHERE task_db_id = :task_db_id
                ORDER BY id
                """
            ),
            {"task_db_id": task_row["id"]},
        ).mappings().all()
        images = [
            ImgItem(
                imgId=row["input_id"],
                imgPath=row["input_path"],
                imgType=row["input_type"],
                dcmType=row["dcm_type"],
            )
            for row in input_rows
        ]
        result = None
        if task_row["task_state"] == 2:
            result = self._load_outcome(connection, task_row["id"], input_rows)
        return {
            "taskState": task_row["task_state"],
            "result": result,
            "failedReason": task_row["failed_reason"],
            "imgs": images,
            "requestId": task_row["request_id"],
            "sysUserId": task_row["sys_user_id"],
        }

    def _load_outcome(self, connection, task_db_id: int, input_rows: list[Mapping]) -> dict:
        reports_db = connection.execute(
            text(
                """
                SELECT input_db_id, report_id, report_type, report_result, roi_data
                FROM algorithm_report
                WHERE task_db_id = :task_db_id
                ORDER BY id
                """
            ),
            {"task_db_id": task_db_id},
        ).mappings().all()
        inputs_by_id = {row["id"]: row for row in input_rows}
        reports: list[dict[str, Any]] = []
        cardiac_ultrasound: list[dict[str, Any]] = []
        ecg: list[dict[str, Any]] = []
        for row in reports_db:
            payload = self._load_json(row["report_result"])
            roi_data = self._load_json(row["roi_data"])
            input_row = inputs_by_id.get(row["input_db_id"])
            reports.append({
                "reportId": row["report_id"],
                "reportType": row["report_type"],
                "reportResult": payload,
                "inputId": input_row["input_id"] if input_row is not None else None,
                "roiData": roi_data,
            })
            if input_row is None:
                continue
            if row["report_type"] == "CU-SUB":
                cardiac_ultrasound.append({
                    "dcmId": input_row["input_id"],
                    "dcmPath": input_row["input_path"],
                    "reportId": row["report_id"],
                    "rois": roi_data or [],
                })
            elif row["report_type"] == "ECG":
                ecg.append({
                    "ecgId": input_row["input_id"],
                    "ecgPath": input_row["input_path"],
                    "reportId": row["report_id"],
                })
        return {
            "reports": reports,
            "cardiacUltrasound": cardiac_ultrasound,
            "ecg": ecg,
        }

    @staticmethod
    def _dump_json(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_json(value: Any) -> Any:
        if value is None or isinstance(value, (dict, list)):
            return value
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)
