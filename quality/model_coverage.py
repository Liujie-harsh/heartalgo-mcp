"""Measurement 脚本、模型权重和验证 DICOM 的覆盖审计。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from echonet_runner import DCM_TYPE_TASKS, SLICE_DIR_MAP


_SCRIPT_WEIGHT_OVERRIDES = {
    "inference_MV_EperA.py": ("Doppler_models", "mvpeak_2c"),
    "inference_TAPSE.py": ("Doppler_models", "tapse_2c"),
}


def _weight_requirement(task: dict) -> tuple[str, str]:
    script = task["script"]
    if script in _SCRIPT_WEIGHT_OVERRIDES:
        return _SCRIPT_WEIGHT_OVERRIDES[script]
    if script == "inference_2D_image.py":
        return "2D_models", str(task["weights"])
    return "Doppler_models", str(task["weights"])


def _dicom_counts(directory: Path) -> tuple[int, int]:
    if not directory.is_dir():
        return 0, 0
    candidates = [
        item
        for item in directory.rglob("*")
        if item.is_file() and item.suffix.lower() == ".dcm"
    ]
    valid = 0
    for item in candidates:
        try:
            with item.open("rb") as source:
                head = source.read(132)
            if len(head) == 132 and head[128:132] == b"DICM":
                valid += 1
        except OSError:
            continue
    return len(candidates), valid


def _weight_status(path: Path) -> tuple[bool, bool, bool]:
    exists = path.is_file()
    if not exists:
        return False, False, False
    try:
        with path.open("rb") as source:
            head = source.read(200)
            lfs_pointer = head.startswith(
                b"version https://git-lfs.github.com/spec/v1"
            )
        is_torch_zip = zipfile.is_zipfile(path)
        if is_torch_zip:
            with zipfile.ZipFile(path) as archive:
                is_torch_zip = bool(archive.namelist())
        is_legacy_pickle = head.startswith(b"\x80") and path.stat().st_size >= 1024
        usable = not lfs_pointer and (is_torch_zip or is_legacy_pickle)
    except OSError:
        return True, False, False
    return True, lfs_pointer, usable


def audit_model_coverage(
    measurement_script_dir: str | Path,
    samples_root: str | Path,
) -> dict:
    """逐切面检查运行脚本、全部必需权重和至少一个验证 DICOM。"""
    measurement_root = Path(measurement_script_dir)
    sample_root = Path(samples_root)
    views: list[dict] = []
    missing_weights: set[str] = set()
    lfs_pointer_weights: set[str] = set()
    invalid_weights: set[str] = set()
    missing_scripts: set[str] = set()
    missing_samples: list[str] = []

    for dcm_type, tasks in DCM_TYPE_TASKS.items():
        script_names = list(dict.fromkeys(str(task["script"]) for task in tasks))
        scripts = []
        for name in script_names:
            exists = (measurement_root / name).is_file()
            scripts.append({"name": name, "exists": exists})
            if not exists:
                missing_scripts.add(name)

        weight_specs: list[tuple[str, str]] = []
        for task in tasks:
            requirement = _weight_requirement(task)
            if requirement not in weight_specs:
                weight_specs.append(requirement)
        weights = []
        for directory, stem in weight_specs:
            filename = f"{stem}_weights.ckpt"
            relative_path = Path("weights") / directory / filename
            exists, lfs_pointer, usable = _weight_status(
                measurement_root / relative_path
            )
            weights.append(
                {
                    "name": filename,
                    "relativePath": relative_path.as_posix(),
                    "exists": exists,
                    "lfsPointer": lfs_pointer,
                    "usable": usable,
                }
            )
            if not usable:
                missing_weights.add(filename)
            if lfs_pointer:
                lfs_pointer_weights.add(filename)
            elif exists and not usable:
                invalid_weights.add(filename)

        relative_sample_dir = SLICE_DIR_MAP[dcm_type]
        sample_count, valid_dicom_count = _dicom_counts(
            sample_root / relative_sample_dir
        )
        if valid_dicom_count == 0:
            missing_samples.append(dcm_type)
        blockers = []
        blockers.extend(
            f"missing script: {item['name']}" for item in scripts if not item["exists"]
        )
        blockers.extend(
            (
                f"Git LFS pointer only: {item['name']}"
                if item["lfsPointer"]
                else f"missing weight: {item['name']}"
            )
            for item in weights
            if not item["usable"]
        )
        if valid_dicom_count == 0:
            blockers.append(
                "missing valid DICOM Part 10 sample"
                if sample_count == 0
                else "validation DICOM files have invalid Part 10 headers"
            )
        views.append(
            {
                "dcmType": dcm_type,
                "sampleDirectory": relative_sample_dir,
                "sampleCount": sample_count,
                "validDicomCount": valid_dicom_count,
                "scripts": scripts,
                "weights": weights,
                "assetsReady": not blockers,
                "blockers": blockers,
            }
        )

    ready_views = sum(item["assetsReady"] for item in views)
    return {
        "schemaVersion": 1,
        "summary": {
            "totalViews": len(views),
            "assetReadyViews": ready_views,
            "allAssetsReady": ready_views == len(views),
            "missingScripts": sorted(missing_scripts),
            "missingWeights": sorted(missing_weights),
            "lfsPointerWeights": sorted(lfs_pointer_weights),
            "invalidWeights": sorted(invalid_weights),
            "missingSampleViews": missing_samples,
        },
        "views": views,
    }
