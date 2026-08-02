from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).with_name("tactic_config.json")


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    group: str
    kind: str
    default: int | bool | str
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    placeholder: str | None = None


CONFIG_GROUPS = (
    ("worker", "工人与寻路"),
    ("core", "核心"),
    ("combat", "战斗分队"),
    ("runtime", "运行"),
)

CONFIG_FIELDS = (
    ConfigField("worker_bfs_enabled", "启用工人 BFS", "worker", "boolean", True),
    ConfigField("bfs_max_steps", "BFS 搜索节点", "worker", "integer", 800, 50, 5000, 50),
    ConfigField("avoid_backtracking", "避免立即回头", "worker", "boolean", True),
    ConfigField("backtrack_penalty", "载矿回头惩罚", "worker", "integer", 10, 0, 100, 1),
    ConfigField("core_movement_enabled", "允许核心移动", "core", "boolean", True),
    ConfigField("prefer_resources_for_core", "核心优先靠近矿点", "core", "boolean", True),
    ConfigField("core_target_enabled", "启用核心目标坐标", "core", "boolean", False),
    ConfigField("core_target_x", "核心目标 X", "core", "integer", 0, -500, 500, 1),
    ConfigField("core_target_y", "核心目标 Y", "core", "integer", 0, -500, 500, 1),
    ConfigField("cargo_wait_distance", "等待载矿工人距离", "core", "integer", 5, 0, 20, 1),
    ConfigField("repair_enabled", "允许修盾", "core", "boolean", True),
    ConfigField("peace_shield_target", "和平修盾目标", "core", "integer", 10, 0, 10, 1),
    ConfigField("combat_shield_target", "战斗修盾目标", "core", "integer", 3, 0, 10, 1),
    ConfigField("resource_reserve", "生产保留金币", "core", "integer", 0, 0, 100, 1),
    ConfigField("population_cap", "人口上限暂停生产", "core", "integer", 20, 0, 20, 1),
    ConfigField(
        "home_team",
        "守家队名单",
        "combat",
        "string",
        "",
        placeholder="例如 V1,R1",
    ),
    ConfigField(
        "attack_team",
        "进攻队名单",
        "combat",
        "string",
        "",
        placeholder="例如 V2,R2",
    ),
    ConfigField(
        "guerrilla_team",
        "游击队名单",
        "combat",
        "string",
        "",
        placeholder="例如 V3,R3",
    ),
    ConfigField("home_patrol_radius", "守家巡逻半径", "combat", "integer", 5, 1, 30, 1),
    ConfigField("attack_target_x", "进攻目标 X", "combat", "integer", 0, -500, 500, 1),
    ConfigField("attack_target_y", "进攻目标 Y", "combat", "integer", 0, -500, 500, 1),
    ConfigField("ranger_attack_range", "游侠开火距离", "combat", "integer", 3, 1, 3, 1),
    ConfigField("map_save_interval_ticks", "地图保存间隔 Tick", "runtime", "integer", 10, 1, 200, 1),
)

_FIELDS_BY_KEY = {field.key: field for field in CONFIG_FIELDS}
_cache_path: Path | None = None
_cache_signature: tuple[int, int] | None = None
_cache_value: dict[str, int | bool | str] | None = None

# Legacy keys removed after combat teams were introduced.
_LEGACY_KEYS = frozenset({
    "vanguard_engage_enabled",
    "ranger_engage_enabled",
})


class ConfigValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("invalid tactic configuration")
        self.errors = errors


def default_config() -> dict[str, int | bool | str]:
    return {field.key: field.default for field in CONFIG_FIELDS}


def config_schema() -> dict[str, Any]:
    return {
        "groups": [{"key": key, "label": label} for key, label in CONFIG_GROUPS],
        "fields": [asdict(field) for field in CONFIG_FIELDS],
    }


def validate_config(
    values: dict[str, Any],
    *,
    base: dict[str, int | bool | str] | None = None,
) -> dict[str, int | bool | str]:
    config = default_config() if base is None else {**default_config(), **base}
    errors: dict[str, str] = {}

    for key in values:
        if key in _LEGACY_KEYS:
            continue
        if key not in _FIELDS_BY_KEY:
            errors[key] = "未知配置项"

    for key, raw_value in values.items():
        if key in _LEGACY_KEYS:
            continue
        field = _FIELDS_BY_KEY.get(key)
        if field is None:
            continue
        if field.kind == "boolean":
            if not isinstance(raw_value, bool):
                errors[key] = "必须是开关值"
                continue
            config[key] = raw_value
            continue

        if field.kind == "string":
            if not isinstance(raw_value, str):
                errors[key] = "必须是文本"
                continue
            if len(raw_value) > 200:
                errors[key] = "不能超过 200 个字符"
                continue
            config[key] = raw_value.strip()
            continue

        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            errors[key] = "必须是整数"
            continue
        if field.minimum is not None and raw_value < field.minimum:
            errors[key] = f"不能小于 {field.minimum}"
            continue
        if field.maximum is not None and raw_value > field.maximum:
            errors[key] = f"不能大于 {field.maximum}"
            continue
        config[key] = raw_value

    if errors:
        raise ConfigValidationError(errors)
    return config


def _path_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def load_config(path: Path = CONFIG_PATH) -> dict[str, int | bool | str]:
    global _cache_path, _cache_signature, _cache_value
    path = Path(path)
    signature = _path_signature(path)
    if path == _cache_path and signature == _cache_signature and _cache_value is not None:
        return dict(_cache_value)

    config = default_config()
    if signature is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config = validate_config(raw)
        except (OSError, json.JSONDecodeError, ConfigValidationError):
            config = default_config()

    _cache_path = path
    _cache_signature = signature
    _cache_value = dict(config)
    return config


def save_config(values: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, int | bool | str]:
    global _cache_path, _cache_signature, _cache_value
    path = Path(path)
    missing = {field.key for field in CONFIG_FIELDS} - values.keys()
    if missing:
        raise ConfigValidationError({key: "缺少配置项" for key in sorted(missing)})
    config = validate_config(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _cache_path = path
    _cache_signature = _path_signature(path)
    _cache_value = dict(config)
    return config
