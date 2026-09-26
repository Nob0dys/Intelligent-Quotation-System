"""历史价目行的唯一构造 / 变更出口。

改造前有 4 处各自拼装 ``HistoryQuote`` 的地方（手工录入、人工确认草稿转正、
价目本导入、seed 示例数据），派生列（``normalized_*`` / ``data_quality``）
靠人工逐处抄写，很容易漏算或算法漂移；表格化编辑还要再引入两个写入点
（新增行、修改行）。因此把"字段 → 派生列"的推导收敛到这里，任何写入路径
都必须经过本模块，避免同一份数据在不同入口算出不同的派生值。

关于派生列与匹配的关系（易踩的坑，务必先读）：

* ``matching.py`` 打分时读的是**原始业务列**（``record["name"]`` /
  ``record["spec"]`` …），归一化在打分时用带 lru_cache 的纯函数现算。
* 因此 ``normalized_*`` 目前只服务于治理口径（``governance_summary`` 按
  ``normalized_name`` 归组），改业务列**不需要**重算它们才能让报价生效。
* ``data_quality`` 同样是"展示/治理口径"列：匹配时 ``matching.py`` 会用
  自己的字段集合重算一份，并不读库里的值。本模块统一成导入路径的口径。

即便如此，仍然统一重算派生列：一是治理页面的重复/冲突分组依赖它，二是
将来若把打分切到预计算列上，写入口已经是对的，不需要回头补数据。
"""

from __future__ import annotations

from typing import Any, Mapping

from .matching import normalize_text
from .models import HistoryQuote

# 表格编辑暴露给用户、可自由增删改的字段。
EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "spec",
    "model",
    "brand",
    "manufacturer",
    "unit",
    "product_code",
    "price",
    "quantity",
    "quote_date",
)

# 由业务列推导、不允许直接写入的派生列。
DERIVED_FIELDS: tuple[str, ...] = (
    "normalized_name",
    "normalized_spec",
    "normalized_model",
    "normalized_unit",
    "normalized_product_code",
    "data_quality",
)

# 系统 / 溯源列：只读展示，编辑时拒绝写入。
SYSTEM_FIELDS: tuple[str, ...] = (
    "id",
    "source_file",
    "source_sheet",
    "source_row",
    "source_priority",
    "created_at",
    "revision",
)

# data_quality 的计分字段（非空占比）。取导入路径的口径：编码有值也算一条
# 有效信息，来源文件表示这行有明确出处。
QUALITY_FIELDS: tuple[str, ...] = (
    "spec",
    "model",
    "manufacturer",
    "unit",
    "product_code",
    "source_file",
)

# 新建行时各业务列的缺省值。
FIELD_DEFAULTS: dict[str, Any] = {
    "name": "",
    "spec": "",
    "model": "",
    "brand": "",
    "manufacturer": "",
    "unit": "",
    "product_code": "",
    "price": 0.0,
    "quantity": None,
    "quote_date": "",
}

_TEXT_FIELDS = frozenset(EDITABLE_FIELDS) - {"price", "quantity"}


class RowValidationError(ValueError):
    """行内数据不合法（缺名称、价格非法等）。消息直接面向用户。"""


def quality_score(values: Mapping[str, Any]) -> float:
    """非空字段占比，0~1。"""
    return sum(bool(values.get(field)) for field in QUALITY_FIELDS) / len(QUALITY_FIELDS)


def coerce_field(field: str, value: Any) -> Any:
    """把来自 HTTP/Excel 的原始值转成列类型。非法值抛 RowValidationError。"""
    if field in _TEXT_FIELDS:
        if value is None:
            return ""
        return str(value).strip()
    if field == "price":
        if value is None or value == "":
            raise RowValidationError("价格不能为空")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise RowValidationError(f"价格不是有效数字: {value!r}") from None
    if field == "quantity":
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise RowValidationError(f"数量不是有效数字: {value!r}") from None
    raise RowValidationError(f"字段不可编辑: {field}")


def normalize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """只取可编辑字段并补缺省值。"""
    values = dict(FIELD_DEFAULTS)
    for field in EDITABLE_FIELDS:
        if field in fields:
            values[field] = coerce_field(field, fields[field])
    return values


def validate(values: Mapping[str, Any]) -> None:
    """业务校验：名称必填、价格非负。不合法直接抛异常。"""
    if not str(values.get("name") or "").strip():
        raise RowValidationError("名称不能为空")
    price = values.get("price")
    if price is None:
        raise RowValidationError("价格不能为空")
    if float(price) < 0:
        raise RowValidationError(f"价格不能为负数: {price}")


def derive_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """由业务列（含 source_file）算出全部派生列。"""
    return {
        "normalized_name": normalize_text(values.get("name")),
        "normalized_spec": normalize_text(values.get("spec")),
        "normalized_model": normalize_text(values.get("model")),
        "normalized_unit": normalize_text(values.get("unit")),
        "normalized_product_code": normalize_text(values.get("product_code")),
        "data_quality": quality_score(values),
    }


# 身份键的构成字段，顺序固定（与 history_dedup_key 的前四项一致）。
IDENTITY_FIELDS: tuple[str, ...] = ("name", "spec", "unit", "manufacturer")


def identity_projection(values: Mapping[str, Any], fields: tuple[str, ...] = IDENTITY_FIELDS) -> tuple:
    """按**指定字段子集**算身份键。

    为什么需要子集：用价目本回填时，文件未必带制造商列。此时文件行算不出完整
    的四元组身份（制造商只能算成空串），拿它去和库里"制造商=某某厂"的行比会
    永远比不上，于是"更新这一行"被误判成"新增一条"。

    所以比对时只用**文件确实提供了的那些身份字段**。字段越少匹配越松，撞上多
    条就交给上层报"需人工指定"，而不是在这里猜。
    """
    return tuple(normalize_text(values.get(field)) for field in fields)


def identity_key_values(name: object, spec: object, unit: object, manufacturer: object) -> tuple:
    """条目的"身份键"：归一化（名称, 参数, 单位, 制造商）四元组，**不含价格**。

    去重键 ``services.history_dedup_key`` 是这个四元组再加一个价格，因此身份键
    恰好是去重键的前四项（有测试守着这个关系）。

    为什么单独要一个不含价格的身份键：用价目本回填数据时，用户改的往往**只有
    价格**。如果身份里含价格，改过价的行会被判成"库里没有这条"从而新增出一条
    重复记录，而不是更新原来那行。价格不参与身份，才能把"同一器材改了价"正确
    识别成"修改这一行"。
    """
    return identity_projection(
        {"name": name, "spec": spec, "unit": unit, "manufacturer": manufacturer}
    )


def identity_key(record: HistoryQuote) -> tuple:
    """记录的身份键（不含价格）。"""
    return identity_projection(fields_of(record))


def diff_fields(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> list[str]:
    """返回 proposed 相对 current 真正变化的可编辑字段名（保持列顺序）。"""
    changed: list[str] = []
    for field in EDITABLE_FIELDS:
        if field not in proposed:
            continue
        if coerce_field(field, proposed[field]) != current.get(field):
            changed.append(field)
    return changed


def fields_of(record: HistoryQuote) -> dict[str, Any]:
    """读出记录的业务列（供重算派生列 / 计算去重键用）。"""
    values = {field: getattr(record, field) for field in EDITABLE_FIELDS}
    values["source_file"] = record.source_file
    return values


def build_history_row(
    row_id: str,
    *,
    source_file: str,
    source_sheet: str = "",
    source_row: int = 0,
    source_priority: int = 0,
    **fields: Any,
) -> HistoryQuote:
    """构造一行 ``HistoryQuote``（派生列由本函数统一算出）。

    调用方只负责 id / 溯源信息 / 业务列，不再手写 ``normalized_*``。
    """
    values = normalize_fields(fields)
    derived_input = {**values, "source_file": source_file}
    return HistoryQuote(
        id=row_id,
        source_file=source_file,
        source_sheet=source_sheet,
        source_row=source_row,
        source_priority=source_priority,
        revision=1,
        **values,
        **derive_fields(derived_input),
    )


def apply_fields(record: HistoryQuote, changes: Mapping[str, Any]) -> list[str]:
    """就地更新一行，返回真正发生变化的字段名列表。

    无变化时返回空列表（调用方据此跳过 UPDATE，也避免无谓地推高 revision）。
    改了业务列就重算派生列并把 ``revision`` 加一。
    """
    changed: list[str] = []
    for field in EDITABLE_FIELDS:
        if field not in changes:
            continue
        value = coerce_field(field, changes[field])
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed.append(field)
    if not changed:
        return []
    for key, value in derive_fields(fields_of(record)).items():
        setattr(record, key, value)
    record.revision = int(record.revision or 1) + 1
    return changed


def row_payload(record: HistoryQuote) -> dict[str, Any]:
    """一行转成 API/前端网格用的字典（业务列 + 只读溯源列）。"""
    payload: dict[str, Any] = {field: getattr(record, field) for field in EDITABLE_FIELDS}
    payload.update(
        {
            "id": record.id,
            "source_file": record.source_file,
            "source_sheet": record.source_sheet,
            "source_row": record.source_row,
            "source_priority": record.source_priority,
            "data_quality": round(float(record.data_quality or 0), 4),
            "revision": int(record.revision or 1),
        }
    )
    return payload


def editable_payload(record: HistoryQuote) -> dict[str, Any]:
    """只取可编辑列的值（用于"合并前 / 合并后"的对比展示）。"""
    return {field: getattr(record, field) for field in EDITABLE_FIELDS}
