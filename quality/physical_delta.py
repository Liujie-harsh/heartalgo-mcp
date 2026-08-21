"""DICOM Ultrasound Region ``PhysicalDelta`` 校准核心。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from algorithm_version import resolve_algorithm_version
from metric_catalog import VIEW_METRICS


PHYSICAL_UNITS = {
    0: "none",
    1: "%",
    2: "dB",
    3: "cm",
    4: "s",
    5: "Hz",
    6: "dB/s",
    7: "cm/s",
    8: "cm2",
    9: "cm2/s",
    10: "cm3",
    11: "cm3/s",
    12: "degree",
}

# 某些 Measurement 模型输出的是上游任务名，而 API 能力目录发布的是
# 下游指标名；仅保留确实等价且属于同一切面的别名。
_CALIBRATION_METRIC_ALIASES = {
    "PLAX": {"lvid", "la"},
}


class PhysicalDeltaError(ValueError):
    """标尺缺失、坐标不适用或校准输入不一致。"""


@dataclass(frozen=True)
class UltrasoundRegion:
    index: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    unit_x_code: int
    unit_y_code: int
    delta_x: float | None
    delta_y: float | None
    reference_pixel_x: int | None = None
    reference_pixel_y: int | None = None
    reference_value_x: float | None = None
    reference_value_y: float | None = None

    def contains(self, points: tuple[tuple[float, float], ...]) -> bool:
        return all(
            self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y
            for x, y in points
        )


@dataclass(frozen=True)
class CalibrationMeasurement:
    name: str
    points: tuple[tuple[float, float], ...]
    axis: str
    gold_standard: float | None = None
    gold_standard_unit: str | None = None
    absolute_tolerance: float | None = None
    relative_tolerance_percent: float | None = None
    region_index: int | None = None


def _dataset_value(dataset, keyword: str, default=None):
    value = getattr(dataset, keyword, default)
    if value is default and hasattr(dataset, "get"):
        value = dataset.get(keyword, default)
    return getattr(value, "value", value)


def extract_ultrasound_regions(dataset) -> list[UltrasoundRegion]:
    """从 pydicom Dataset（或等价对象）读取 Ultrasound Region 标尺。"""
    sequence = _dataset_value(dataset, "SequenceOfUltrasoundRegions", None)
    if not sequence:
        raise PhysicalDeltaError("DICOM 缺少 Sequence of Ultrasound Regions (0018,6011)")
    regions: list[UltrasoundRegion] = []
    for index, item in enumerate(sequence):
        required = {
            "min_x": _dataset_value(item, "RegionLocationMinX0", None),
            "max_x": _dataset_value(item, "RegionLocationMaxX1", None),
            "min_y": _dataset_value(item, "RegionLocationMinY0", None),
            "max_y": _dataset_value(item, "RegionLocationMaxY1", None),
            "unit_x_code": _dataset_value(item, "PhysicalUnitsXDirection", None),
            "unit_y_code": _dataset_value(item, "PhysicalUnitsYDirection", None),
        }
        if any(value is None for value in required.values()):
            raise PhysicalDeltaError(f"超声区域 {index} 缺少位置或物理单位标签")
        delta_x = _dataset_value(item, "PhysicalDeltaX", None)
        delta_y = _dataset_value(item, "PhysicalDeltaY", None)
        regions.append(
            UltrasoundRegion(
                index=index,
                min_x=int(required["min_x"]),
                max_x=int(required["max_x"]),
                min_y=int(required["min_y"]),
                max_y=int(required["max_y"]),
                unit_x_code=int(required["unit_x_code"]),
                unit_y_code=int(required["unit_y_code"]),
                delta_x=float(delta_x) if delta_x is not None else None,
                delta_y=float(delta_y) if delta_y is not None else None,
                reference_pixel_x=_optional_int(
                    _dataset_value(item, "ReferencePixelX0", None)
                ),
                reference_pixel_y=_optional_int(
                    _dataset_value(item, "ReferencePixelY0", None)
                ),
                reference_value_x=_optional_float(
                    _dataset_value(item, "ReferencePixelPhysicalValueX", None)
                ),
                reference_value_y=_optional_float(
                    _dataset_value(item, "ReferencePixelPhysicalValueY", None)
                ),
            )
        )
    return regions


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value) -> float | None:
    return float(value) if value is not None else None


def _select_region(
    regions: list[UltrasoundRegion], measurement: CalibrationMeasurement
) -> UltrasoundRegion:
    if measurement.region_index is not None:
        selected = next(
            (item for item in regions if item.index == measurement.region_index), None
        )
        if selected is None:
            raise PhysicalDeltaError(
                f"未找到 regionIndex={measurement.region_index} 的超声区域"
            )
        if not selected.contains(measurement.points):
            raise PhysicalDeltaError("模型坐标不在指定超声区域内")
        return selected
    candidates = [item for item in regions if item.contains(measurement.points)]
    if not candidates:
        raise PhysicalDeltaError("没有同时包含两个模型坐标的超声区域")
    if len(candidates) > 1:
        raise PhysicalDeltaError(
            "多个超声区域包含模型坐标，请在校准项中显式设置 regionIndex"
        )
    return candidates[0]


def _converted_value(
    region: UltrasoundRegion, measurement: CalibrationMeasurement
) -> tuple[float, str]:
    axis = measurement.axis.lower()
    if axis == "absolute_x":
        if (
            region.delta_x is None
            or region.reference_pixel_x is None
            or region.reference_value_x is None
        ):
            raise PhysicalDeltaError(
                "absolute_x 需要 PhysicalDeltaX 和 X 方向参考像素/物理值"
            )
        x, _ = measurement.points[0]
        value = region.reference_value_x + (
            x - region.reference_pixel_x
        ) * region.delta_x
        return value, PHYSICAL_UNITS.get(
            region.unit_x_code, f"code:{region.unit_x_code}"
        )
    if axis == "absolute_y":
        if (
            region.delta_y is None
            or region.reference_pixel_y is None
            or region.reference_value_y is None
        ):
            raise PhysicalDeltaError(
                "absolute_y 需要 PhysicalDeltaY 和 Y 方向参考像素/物理值"
            )
        _, y = measurement.points[0]
        value = region.reference_value_y + (
            y - region.reference_pixel_y
        ) * region.delta_y
        return value, PHYSICAL_UNITS.get(
            region.unit_y_code, f"code:{region.unit_y_code}"
        )

    (x1, y1), (x2, y2) = measurement.points
    if axis == "x":
        if region.delta_x is None:
            raise PhysicalDeltaError("超声区域缺少 PhysicalDeltaX")
        return abs(x2 - x1) * abs(region.delta_x), PHYSICAL_UNITS.get(
            region.unit_x_code, f"code:{region.unit_x_code}"
        )
    if axis == "y":
        if region.delta_y is None:
            raise PhysicalDeltaError("超声区域缺少 PhysicalDeltaY")
        return abs(y2 - y1) * abs(region.delta_y), PHYSICAL_UNITS.get(
            region.unit_y_code, f"code:{region.unit_y_code}"
        )
    if axis != "euclidean":
        raise PhysicalDeltaError(
            "axis 必须是 x、y、euclidean、absolute_x 或 absolute_y"
        )
    if region.delta_x is None or region.delta_y is None:
        raise PhysicalDeltaError("二维距离需要 PhysicalDeltaX 和 PhysicalDeltaY")
    if region.unit_x_code != region.unit_y_code:
        raise PhysicalDeltaError("二维距离的 X/Y 物理单位不一致")
    dx = (x2 - x1) * region.delta_x
    dy = (y2 - y1) * region.delta_y
    return math.hypot(dx, dy), PHYSICAL_UNITS.get(
        region.unit_x_code, f"code:{region.unit_x_code}"
    )


def calibrate_measurement(
    regions: list[UltrasoundRegion], measurement: CalibrationMeasurement
) -> dict:
    """将一条模型像素线段换算为物理量，并与人工金标准比较。"""
    axis = measurement.axis.lower()
    expected_points = 1 if axis in {"absolute_x", "absolute_y"} else 2
    if len(measurement.points) != expected_points:
        raise PhysicalDeltaError(
            f"axis={measurement.axis!r} 要求 {expected_points} 个模型坐标"
        )
    if not measurement.name.strip() or any(
        not math.isfinite(coordinate)
        for point in measurement.points
        for coordinate in point
    ):
        raise PhysicalDeltaError("校准名称不能为空，模型坐标必须是有限数值")
    tolerance_values = (
        measurement.absolute_tolerance,
        measurement.relative_tolerance_percent,
    )
    if any(value is not None and value < 0 for value in tolerance_values):
        raise PhysicalDeltaError("临床容差不能为负数")
    numeric_values = (measurement.gold_standard, *tolerance_values)
    if any(value is not None and not math.isfinite(value) for value in numeric_values):
        raise PhysicalDeltaError("金标准和临床容差必须是有限数值")
    if measurement.gold_standard is not None and not measurement.gold_standard_unit:
        raise PhysicalDeltaError("提供金标准时必须同时提供 goldStandardUnit")
    region = _select_region(regions, measurement)
    if any(
        value is not None and not math.isfinite(value)
        for value in (region.delta_x, region.delta_y)
    ):
        raise PhysicalDeltaError("DICOM PhysicalDelta 必须是有限数值")
    converted, unit = _converted_value(region, measurement)
    if measurement.gold_standard_unit and measurement.gold_standard_unit != unit:
        raise PhysicalDeltaError(
            f"金标准单位 {measurement.gold_standard_unit!r} 与 DICOM 单位 {unit!r} 不一致"
        )

    absolute_error = None
    relative_error = None
    within_tolerance = None
    if measurement.gold_standard is not None:
        absolute_error = abs(converted - measurement.gold_standard)
        if measurement.gold_standard != 0:
            relative_error = absolute_error / abs(measurement.gold_standard) * 100
        checks: list[bool] = []
        if measurement.absolute_tolerance is not None:
            checks.append(absolute_error <= measurement.absolute_tolerance)
        if measurement.relative_tolerance_percent is not None:
            checks.append(
                relative_error is not None
                and relative_error <= measurement.relative_tolerance_percent
            )
        within_tolerance = all(checks) if checks else None

    return {
        "name": measurement.name,
        "points": [[x, y] for x, y in measurement.points],
        "axis": measurement.axis,
        "regionIndex": region.index,
        "convertedValue": converted,
        "unit": unit,
        "goldStandard": measurement.gold_standard,
        "absoluteError": absolute_error,
        "relativeErrorPercent": relative_error,
        "withinTolerance": within_tolerance,
        "rawScale": {
            "regionBounds": {
                "minX": region.min_x,
                "maxX": region.max_x,
                "minY": region.min_y,
                "maxY": region.max_y,
            },
            "physicalUnitsXCode": region.unit_x_code,
            "physicalUnitsYCode": region.unit_y_code,
            "physicalUnitX": PHYSICAL_UNITS.get(region.unit_x_code),
            "physicalUnitY": PHYSICAL_UNITS.get(region.unit_y_code),
            "physicalDeltaX": region.delta_x,
            "physicalDeltaY": region.delta_y,
            "referencePixelX": region.reference_pixel_x,
            "referencePixelY": region.reference_pixel_y,
            "referenceValueX": region.reference_value_x,
            "referenceValueY": region.reference_value_y,
        },
    }


def _measurement_from_dict(item: dict) -> CalibrationMeasurement:
    try:
        points = tuple(
            (float(point[0]), float(point[1])) for point in item["points"]
        )
        if len(points) not in {1, 2}:
            raise ValueError
        return CalibrationMeasurement(
            name=str(item["name"]),
            points=points,
            axis=str(item["axis"]),
            gold_standard=_optional_float(item.get("goldStandard")),
            gold_standard_unit=item.get("goldStandardUnit"),
            absolute_tolerance=_optional_float(item.get("absoluteTolerance")),
            relative_tolerance_percent=_optional_float(
                item.get("relativeTolerancePercent")
            ),
            region_index=_optional_int(item.get("regionIndex")),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise PhysicalDeltaError(
            "校准项必须包含 name、两个 points 和 axis"
        ) from exc


def build_calibration_report(
    *,
    regions: list[UltrasoundRegion],
    source_sha256: str,
    manifest: dict,
) -> dict:
    """生成不包含路径或患者字段的可审计校准报告。"""
    if not regions:
        raise PhysicalDeltaError("DICOM 未提供可用的超声区域标尺")
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in source_sha256
    ):
        raise PhysicalDeltaError("source_sha256 必须是 64 位十六进制 SHA-256")
    algorithm_version = resolve_algorithm_version(
        {"ALGORITHM_VERSION": str(manifest.get("algorithmVersion", ""))},
        use_fake=False,
    )
    dcm_type = manifest.get("dcmType")
    if dcm_type not in VIEW_METRICS:
        raise PhysicalDeltaError("校准清单必须包含受支持的 dcmType")
    raw_measurements = manifest.get("measurements")
    if not isinstance(raw_measurements, list) or not raw_measurements:
        raise PhysicalDeltaError("校准清单必须包含非空 measurements 列表")
    parsed_measurements = [_measurement_from_dict(item) for item in raw_measurements]
    for measurement in parsed_measurements:
        allowed_metrics = set(VIEW_METRICS[dcm_type]) | _CALIBRATION_METRIC_ALIASES.get(
            dcm_type, set()
        )
        if measurement.name.strip().lower() not in allowed_metrics:
            raise PhysicalDeltaError(
                f"校准指标 {measurement.name!r} 不属于切面 {dcm_type!r}"
            )
        if measurement.gold_standard is None:
            raise PhysicalDeltaError("每项校准测量都必须提供人工金标准")
        if (
            measurement.absolute_tolerance is None
            and measurement.relative_tolerance_percent is None
        ):
            raise PhysicalDeltaError("每项校准测量都必须提供临床批准的容差")
    measurements = [
        calibrate_measurement(regions, measurement)
        for measurement in parsed_measurements
    ]
    evaluated = [
        item for item in measurements if item["withinTolerance"] is not None
    ]
    with_gold = [
        item for item in measurements if item["goldStandard"] is not None
    ]
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceSha256": source_sha256.lower(),
        "algorithmVersion": algorithm_version,
        "dcmType": dcm_type,
        "summary": {
            "total": len(measurements),
            "withGoldStandard": len(with_gold),
            "passed": sum(item["withinTolerance"] is True for item in evaluated),
            "failed": sum(item["withinTolerance"] is False for item in evaluated),
            "unevaluated": len(measurements) - len(evaluated),
        },
        "measurements": measurements,
    }
