from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from sqlalchemy import delete, func, select, text

from . import database
from .database import init_db
from .excel_service import parse_quote_workbook
from .matching import (
    bigram_dice,
    distinct_manufacturer_options,
    grade_marker,
    match_line,
    name_core,
    normalize_code,
    normalize_text,
    spec_values,
    _spec_features,
    _values_overlap,
    warmup_match_caches,
)
from .category import category_compatible, infer_category_from_name
from .models import (
    AuditEvent,
    Customer,
    CustomerRequirement,
    HistoryQuote,
    QuoteJob,
    QuoteLine,
    QuoteOption,
    SystemMeta,
    User,
)
from .security import hash_password


# 候选可选下限：低于该分数不默认带出任何方案，也不视为“有匹配”。
# 低于 55(RELIABLE_THRESHOLD) 但仍 >=本值的候选按低置信复核展示。
OPTION_FLOOR = 40.0
# 非赛特尔来源的默认选中下限（一键导出覆盖率优先，30-40 分行自动带价但标低置信）。
AUTOSELECT_FLOOR = 30.0
# 赛特尔价目本为规则指定优先来源（“有赛特尔选赛特尔”），其候选下限放宽到 20 分。
SAITEL_FLOOR = 20.0
# 名称核心词自动带价门槛：相似度低于该值视为无关产品，不自动带价。
MATCH_NAME_FLOOR = 0.30
# 赛特尔“前缀/材质变体”放宽的相似度门槛（直尺↔钢直尺 0.667 通过；
# 槽码↔钩码 0.333、演示器↔实验器 0.625 等一字之差/语义变体被拦）。
SAITEL_NAME_FLOOR = 0.65
# 修饰词变体：行名与记录名去掉修饰词后相同但修饰词集合不同，
# 视为不同产品（演示斜面小车≠斜面小车、新型船闸模型≠船闸模型），不自动带价。
# 与 matching.MODIFIER_PENALTY_PAT 保持同步——同一套理化生教学仪器修饰词词表。
MODE_TERMS_RE = re.compile(
    r"(演示|实验|图形|内能|新型|高中|初中|小学|学生用|教师用|数显|指针|液晶|"
    r"高压|低压|可调|精密|简易|普通|袖珍|微量|便携|台式|立式|手持|"
    r"不锈钢|铜质|铁质|塑料|玻璃|木质|单面|双面|电磁式|永磁式|"
    r"数字式|模拟式|普通型|高精度)"
)
# 赛特尔优先：仅“赛特尔25年.xls”价目本为最高优先级来源（有赛特尔25年记录时
# 优先默认选中）；其余所有文件（普教/包1-4/标准答案等）优先级相同。
SAITEL_MARK = "赛特尔25年"


def mode_conflict(line_core: str, record_core: str) -> bool:
    if line_core == record_core:
        return False
    stripped_line = MODE_TERMS_RE.sub("", line_core)
    stripped_record = MODE_TERMS_RE.sub("", record_core)
    if not stripped_line or stripped_line != stripped_record:
        return False
    # 名称以 演示器↔实验器 互变视为等价（摩擦力演示器=摩擦力实验器）
    if (line_core.endswith("演示器") and record_core.endswith("实验器")) or (
        line_core.endswith("实验器") and record_core.endswith("演示器")
    ):
        return False
    # 包埋↔浸制 互变视为等价（蟾蜍包埋标本=蟾蜍浸制标本）
    if ("包埋" in line_core and "浸制" in record_core) or (
        "浸制" in line_core and "包埋" in record_core
    ):
        return False
    return True


def is_saitel(source_file: object) -> bool:
    return SAITEL_MARK in str(source_file or "")


def robust_median(prices: list[float]) -> float:
    """截尾稳健中位数：去掉最高 25% 的离群高价后取中位数，防止
    包1-4 等高价来源污染同核心词组的价格基准。"""
    values = sorted(prices)
    if len(values) >= 5:
        values = values[: max(3, int(len(values) * 0.75))]
    return median(values) if values else 0.0


def _price_clusters(sorted_items, line_name: str = "", line_code: str = ""):
    """按价格把候选分成簇（与簇首价差 >30% 视为不同价位水平，非链式比较，
    避免 2.0→2.31→3.0 连环成簇）。簇内做类目一致性过滤：与询价类目
    不一致的候选不进簇（防止正确价段被异类产品稀释）。"""
    clusters: list[list] = []
    if line_name or line_code:
        filtered = [
            item for item in sorted_items
            if category_compatible(line_code, item.record.get("product_code"),
                                   line_name, item.record.get("name"))[0]
        ]
        # 类目过滤后为空时退回原列表（类目判不出时不阻塞）
        if filtered:
            sorted_items = filtered
    for item in sorted_items:
        price = float(item.record.get("price", 0))
        if clusters:
            first = float(clusters[-1][0].record.get("price", 0))
            if abs(price - first) / max(first, 1e-9) <= 0.30:
                clusters[-1].append(item)
                continue
        clusters.append([item])
    return clusters


def diversify_price_levels(items, defaults: set, line_core_name: str) -> list:
    """候选价位聚类重排：默认选中排最前；随后优先把“与询价同名的主组”
    各价位簇依次排入（不同价位水平都有代表），再轮转其余组，避免正确价
    被同词高分簇或异组候选淹没。"""
    ordered = [item for item in items if item.record["id"] in defaults]
    rest = [item for item in items if item.record["id"] not in defaults]
    groups: dict[str, list] = {}
    keys: list[str] = []
    for item in rest:
        key = name_core(item.record.get("name", "")) or "?"
        if key not in groups:
            groups[key] = []
            keys.append(key)
        groups[key].append(item)
    remaining: dict[str, list] = {}
    for key in keys:
        # 簇内做类目一致性：只留与询价类目一致的候选，异类产品自然被挤掉
        remaining[key] = _price_clusters(
            sorted(groups[key], key=lambda it: it.record.get("price", 0)),
            line_name=line_core_name,
        )

    def emit_round(keys_iter) -> None:
        for key in keys_iter:
            clusters = remaining.get(key)
            if not clusters:
                continue
            ordered.append(clusters[0].pop(0))
            if not clusters[0]:
                clusters.pop(0)
            if not clusters:
                del remaining[key]

    primary = line_core_name if line_core_name in remaining else None
    if primary:
        # 主组簇轮转：每轮从每个价位簇各取一个代表，保证不同价位尽快进入 TOP 区。
        while primary in remaining and remaining[primary]:
            clusters = remaining[primary]
            for cluster in list(clusters):
                ordered.append(cluster.pop(0))
                if not cluster:
                    clusters.remove(cluster)
            if not clusters:
                del remaining[primary]
    other_keys = [key for key in keys if key != primary and key in remaining]
    while any(remaining.values()):
        emit_round(other_keys)
    return ordered


def _is_subsequence(short: str, long: str) -> bool:
    """短串字符按序出现在长串中（非连续子串）。

    ``塑料球`` 是 ``塑料小球`` 的子序列（塑→料→球 按序出现），
    但 ``槽码`` 不是 ``钩码`` 的子序列。用于 name_allows_autoselect
    的包含检查——连续子串检查会漏掉"插入修饰字"的变体（塑料小球↔塑料球）。
    """
    if not short:
        return True
    it = iter(long)
    return all(ch in it for ch in short)


def name_allows_autoselect(line_name: str, record_name: str, source_file: object = "") -> bool:
    """名称核心词门槛：相似度不足、学段不一致或记录为语义变体时不允许自动带价。

    记录核心词未被询价核心词包含时视为变体错配（条形强磁体↔蹄形强磁体），
    但赛特尔价目本的“前缀/材质变体”（直尺↔钢直尺）放宽允许，避免错失其正确价。
    学段标记（高中学生电源 vs 学生电源）不一致时不自动带价。
    """
    line_core = name_core(line_name)
    record_core = name_core(record_name)
    if mode_conflict(line_core, record_core):
        return False
    line_marker = grade_marker(line_core)
    record_marker = grade_marker(record_core)
    if line_marker and record_marker and line_marker != record_marker:
        return False
    if line_marker and not record_marker:
        return False
    if bigram_dice(line_core, record_core) < MATCH_NAME_FLOOR:
        return False
    # 等价变体：包埋↔浸制（蟾蜍包埋标本=蟾蜍浸制标本=蟾蜍标本）、演示器↔实验器
    # 直接放行，不因相似度门槛拦截。任一侧含 包埋/浸制 时剥词比较核心。
    if ("包埋" in line_core or "浸制" in line_core) or (
        "包埋" in record_core or "浸制" in record_core
    ):
        line_core2 = line_core.replace("包埋", "").replace("浸制", "")
        record_core2 = record_core.replace("包埋", "").replace("浸制", "")
        if line_core2 and line_core2 == record_core2:
            return True
    if (line_core.endswith("演示器") and record_core.endswith("实验器")) or (
        line_core.endswith("实验器") and record_core.endswith("演示器")
    ):
        return True
    # 包含检查：记录名是询价名的连续子串或子序列（塑料球 ⊂ 塑料小球），
    # 或询价名是记录名的前缀（白卡纸带四方格 ⊂ 白卡纸带四方格、双面胶…）。
    if bool(record_core) and (
        record_core in line_core
        or _is_subsequence(record_core, line_core)
        or record_core.startswith(line_core)
    ):
        return True
    # 赛特尔放宽：前缀/材质变体（直尺↔钢直尺）允许，但需相似度达到
    # SAITEL_NAME_FLOOR，拦截 槽码↔钩码 等一字之差错配；功能变体
    # （计算器↔图形计算器）由 mode_conflict 拦截。
    return is_saitel(source_file) and bigram_dice(line_core, record_core) >= SAITEL_NAME_FLOOR


def history_dedup_key(name: object, spec: object, unit: object, manufacturer: object, price: object) -> tuple:
    """Duplicate key: normalized (name, spec, unit, manufacturer, price) quintuple."""
    try:
        price_value = round(float(price), 4)
    except (TypeError, ValueError):
        price_value = 0.0
    return (
        normalize_text(name),
        normalize_text(spec),
        normalize_text(unit),
        normalize_text(manufacturer),
        price_value,
    )


def audit(db, user_id: int, action: str, entity_type: str, entity_id: object, detail: dict | None = None):
    db.add(
        AuditEvent(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            detail=detail or {},
        )
    )


def get_history_version(db) -> int:
    """Read the current history version (0 if missing).  Incremented on any
    HistoryQuote write so cached candidate pools can be invalidated."""
    row = db.get(SystemMeta, "history_version")
    if not row:
        return 0
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return 0


def bump_history_version(db) -> None:
    row = db.get(SystemMeta, "history_version")
    if row is None:
        row = SystemMeta(key="history_version", value="1")
        db.add(row)
    else:
        try:
            row.value = str(int(row.value) + 1)
        except (TypeError, ValueError):
            row.value = "1"


def seed_database(seed_history: bool = True) -> None:
    init_db()
    db = database.SessionLocal()
    try:
        admin_name = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
        if not db.scalar(select(User).where(User.username == admin_name)):
            db.add(
                User(
                    username=admin_name,
                    display_name="系统管理员",
                    role="admin",
                    password_hash=hash_password(os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")),
                )
            )
        quote_name = os.getenv("DEFAULT_QUOTE_USERNAME", "quote")
        if not db.scalar(select(User).where(User.username == quote_name)):
            db.add(
                User(
                    username=quote_name,
                    display_name="报价员",
                    role="quote",
                    password_hash=hash_password(os.getenv("DEFAULT_QUOTE_PASSWORD", "quote123")),
                )
            )
        db.flush()
        if not db.scalar(select(func.count(Customer.id))):
            ordinary = Customer(name="普通客户示例", customer_type="ordinary", notes="使用标准报价策略")
            special = Customer(name="特殊要求客户示例", customer_type="special", notes="必选参数不满足时必须复核")
            vip = Customer(name="VIP客户示例", customer_type="vip", notes="优先展示协议与偏好制造商")
            db.add_all([ordinary, special, vip])
            db.flush()
            db.add(
                CustomerRequirement(
                    customer_id=special.id,
                    attribute_name="材质",
                    operator="contains",
                    value="不锈钢",
                    required=True,
                    notes="演示必选要求，可由管理员维护",
                )
            )
        db.commit()

        if seed_history and not db.scalar(select(func.count(HistoryQuote.id))):
            seed_path = Path(
                os.getenv(
                    "HISTORY_SEED_PATH",
                    Path(__file__).resolve().parents[2] / "public" / "demo" / "history.json",
                )
            )
            if seed_path.exists():
                payload = json.loads(seed_path.read_text(encoding="utf-8"))
                seen_keys: set[tuple] = set()
                for record in payload.get("records", []):
                    key = history_dedup_key(
                        record.get("name", ""),
                        record.get("spec", ""),
                        record.get("unit", ""),
                        record.get("manufacturer", ""),
                        record.get("price", 0),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    quality_fields = ["spec", "model", "manufacturer", "unit", "quoteDate", "sourceFile"]
                    quality = sum(bool(record.get(item)) for item in quality_fields) / len(quality_fields)
                    db.add(
                        HistoryQuote(
                            id=str(record["id"]),
                            source_file=record.get("sourceFile", ""),
                            source_sheet=record.get("sourceSheet", ""),
                            source_row=int(record.get("sourceRow", 0)),
                            name=record.get("name", ""),
                            normalized_name=record.get("normalizedName", ""),
                            spec=record.get("spec", ""),
                            normalized_spec=record.get("normalizedSpec", ""),
                            product_code=record.get("productCode", ""),
                            normalized_product_code=record.get("normalizedProductCode", ""),
                            model=record.get("model", ""),
                            normalized_model=record.get("normalizedModel", ""),
                            brand=record.get("brand", ""),
                            manufacturer=record.get("manufacturer", ""),
                            unit=record.get("unit", ""),
                            normalized_unit=record.get("normalizedUnit", ""),
                            quantity=record.get("quantity"),
                            price=float(record.get("price", 0)),
                            quote_date=record.get("quoteDate", ""),
                            source_priority=int(record.get("sourcePriority", 0)),
                            data_quality=quality,
                        )
                    )
                db.commit()
    finally:
        db.close()


def _history_dict(record: HistoryQuote) -> dict:
    return {
        "id": record.id,
        "source_file": record.source_file,
        "source_sheet": record.source_sheet,
        "source_row": record.source_row,
        "name": record.name,
        "spec": record.spec,
        "product_code": record.product_code,
        "model": record.model,
        "brand": record.brand,
        "manufacturer": record.manufacturer,
        "unit": record.unit,
        "price": record.price,
        "quote_date": record.quote_date,
        "source_priority": record.source_priority,
        "data_quality": record.data_quality,
    }


# 配置加价规则：老编号体系的基础记录不含某配置时的补差价（用户确认的行业规则）。
# 21032 滑轮组：带"可止动"配置时 9 元基础价 + 6 元/套 = 15 元（老课标优先）。
CONFIG_PRICE_RULES: tuple[dict, ...] = (
    {
        "record_code": "21032",
        "keywords": ("止动", "可止动", "可卡"),
        "delta": 6.0,
        "note": "含可止动滑轮，+6元/套",
    },
)


def config_price_delta(line: dict, record: dict) -> tuple[float, str]:
    """返回 (加价金额, 说明)。规则按"记录编号 + 询价配置关键词"匹配。"""
    code = normalize_code(record.get("product_code"))
    text = normalize_text(f"{line.get('name', '')} {line.get('spec', '')}")
    for rule in CONFIG_PRICE_RULES:
        if rule.get("record_code") and code != rule["record_code"]:
            continue
        if any(keyword in text for keyword in rule["keywords"]):
            return float(rule["delta"]), str(rule["note"])
    return 0.0, ""


def _history_session(job):
    """任务级绑定（B1）：返回读取价目本的会话。

    未绑定库、或绑定的库文件已不存在时回退到系统默认库，保证历史任务与
    未选库的报价行为与改造前完全一致。
    """
    key = (getattr(job, "database_key", "") or "").strip()
    if not key or not (database.DATA_DIR / f"{key}.db").exists():
        return database.SessionLocal()
    return database.session_for_key(key)


def _record_snapshot(record: dict) -> dict:
    """把匹配用的历史记录转成 QuoteOption 的落库快照字段。

    跨库报价时历史记录不在当前库中，``history_quote`` 关联为空，导出与前端
    展示只能依赖这份快照。
    """
    return {
        "record_source_file": record.get("source_file") or "",
        "record_source_sheet": record.get("source_sheet") or "",
        "record_source_row": int(record.get("source_row") or 0),
        "record_name": record.get("name") or "",
        "record_spec": record.get("spec") or "",
        "record_model": record.get("model") or "",
        "record_brand": record.get("brand") or "",
        "record_manufacturer": record.get("manufacturer") or "",
        "record_unit": record.get("unit") or "",
        "record_product_code": record.get("product_code") or "",
        "record_price": float(record.get("price") or 0),
        "record_quote_date": record.get("quote_date") or "",
    }


def process_job(job_id: str) -> None:
    db = database.SessionLocal()
    history_db = None
    try:
        job = db.get(QuoteJob, job_id)
        if not job:
            return
        job.status = "matching"
        job.progress = 3
        job.error_message = ""
        db.commit()
        parsed_lines = parse_quote_workbook(job.source_file_path)
        db.execute(delete(QuoteLine).where(QuoteLine.job_id == job_id))
        db.commit()
        history_db = _history_session(job)
        # 显式按 rowid（入库顺序）加载：厂商"首现顺序"映射依赖这个顺序，
        # 库不变时报价1/2/3 的厂商次序完全可复现。
        history = [
            _history_dict(item)
            for item in history_db.scalars(select(HistoryQuote).order_by(text("rowid"))).all()
        ]
        # 厂商首现顺序映射（XJHY 宽表 品牌1/2/3 的列顺序即 赛特尔→瑞仕达→德欧）：
        # 报价1/2/3 按厂商在价目库中的出现顺序固定输出。无制造商记录不参排，
        # 恒排在已知厂商之后（赛特尔专库等场景行为与改造前一致）。
        mfr_appearance: dict[str, int] = {}
        for item in history:
            identity = normalize_text(item.get("manufacturer") or item.get("brand"))
            if identity and identity not in mfr_appearance:
                mfr_appearance[identity] = len(mfr_appearance)
        # 冷启动预热：把历史库元数据一次性算进 LRU 缓存，后续所有行
        # 的候选池排序/打分全部缓存命中（1675 行 × 全库 ≈ 500 万次
        # 重复解析 → 预热后全部 O(1) 命中）。
        warmup_match_caches(history)
        # 任务级学段投票：询价表的配备标准编号（分类代码）命中的记录学段做多数
        # 投票（≥60% 且 ≥3 票），作为同名候选的学段偏好——整表通常是同一学段的
        # 配备清单（如高中 龙岩表 15/15 命中高中物理新课标），避免高中清单默认
        # 选中初中版（直联泵 220 vs 340、感应圈 380 vs 220）。
        # 任务级学段投票：只有命中"高中新课标"（高中物理/化学/生物新课标）的记录
        # 才算强证据——高中物理新课标编号列完整（483 行有 84 个编号），而初中
        # 新课标编号列与高中共享/混编且老价目本 5 位编号在高中老表也大量存在，
        # 命中不代表清单学段。龙岩（高中清单）15 个高中新课标命中 → 高中；
        # 预算清单（初中）与科学仪器（混合/无编号）→ 不产生偏好。
        code_grades_map: dict[str, set[str]] = {}
        for item in history:
            key = normalize_code(item.get("product_code"))
            if not key:
                continue
            sheet = str(item.get("source_sheet") or "")
            if "新课标" in sheet:
                code_grades_map.setdefault(key, set()).add(grade_marker(sheet))
        grade_votes: dict[str, int] = {}
        for raw_line in parsed_lines:
            key = normalize_code(raw_line.get("product_code"))
            if not key:
                continue
            if "高中" in code_grades_map.get(key, set()):
                grade_votes["高中"] = grade_votes.get("高中", 0) + 1
        job_grade = "高中" if grade_votes.get("高中", 0) >= 3 else ""
        customer = db.get(Customer, job.customer_id) if job.customer_id else None
        # Structured requirements apply to special and VIP customers only.
        requirements = []
        if customer and customer.customer_type in ("special", "vip"):
            requirements = [
                {
                    "attribute_name": item.attribute_name,
                    "operator": item.operator,
                    "value": item.value,
                    "unit": item.unit,
                    "required": item.required,
                }
                for item in db.scalars(
                    select(CustomerRequirement).where(CustomerRequirement.customer_id == job.customer_id)
                ).all()
            ]
        preferred = {
            normalize_text(item)
            for item in (customer.preferred_manufacturers if customer else [])
            if normalize_text(item)
        }
        matched = review = unmatched = 0
        for index, raw_line in enumerate(parsed_lines):
            # 候选基于全历史库：精确同名/同码与通用名高分候选统一评分（候选池合并）
            candidates = match_line(raw_line, history, requirements)
            if preferred:
                def preference_bonus(candidate):
                    identity = normalize_text(
                        candidate.record.get("manufacturer") or candidate.record.get("brand")
                    )
                    is_preferred = identity in preferred
                    if is_preferred and "VIP偏好制造商" not in candidate.reasons:
                        candidate.reasons.append("VIP偏好制造商")
                    return 3 if is_preferred else 0

                candidates.sort(
                    key=lambda candidate: (
                        candidate.score + preference_bonus(candidate),
                        candidate.score,
                        candidate.component_scores.get("参数", 0),
                    ),
                    reverse=True,
                )
            best = candidates[0] if candidates else None
            warnings = list(best.warnings) if best else ["没有找到可靠候选"]
            if (
                len(candidates) > 1
                and abs(candidates[0].score - candidates[1].score) < 0.01
                and float(candidates[0].record.get("price", 0))
                != float(candidates[1].record.get("price", 0))
            ):
                warnings.append("同分多价：最高分候选存在不同历史价格")
            needs_margin_approval = bool(
                customer
                and customer.customer_type == "vip"
                and customer.discount_percent > 0
                and customer.minimum_margin_percent > 0
            )
            if needs_margin_approval:
                warnings.append(
                    f"BLOCK: VIP折扣需核对最低毛利线（{customer.minimum_margin_percent:g}%）"
                )
            best_score = best.score if best else 0.0
            # 精确同名候选放宽门槛：长规格文本会稀释参数分（分子结构模型 30 分档），
            # 但同名+有价仍应给"待复核"方案而不是判无匹配走估算。
            exact_name_best = bool(
                best
                and name_core(best.record.get("name", ""))
                and name_core(best.record.get("name", "")) == name_core(raw_line.get("name", ""))
            )
            effective_floor = min(OPTION_FLOOR, 25.0) if exact_name_best else OPTION_FLOOR
            if not best or best_score < effective_floor:
                status = "unmatched"
                confidence = "unreliable"
                unmatched += 1
            elif best.confidence == "unreliable" or best.confidence in ("review", "low", "medium") or needs_margin_approval:
                status = "review"
                confidence = "low" if best.confidence == "unreliable" else best.confidence
                review += 1
            else:
                status = "suggested"
                confidence = "high"
                matched += 1
            line = QuoteLine(
                job_id=job.id,
                status=status,
                confidence=confidence,
                recommended_score=best.score if best else 0,
                warnings=warnings,
                **raw_line,
            )
            db.add(line)
            db.flush()
            line_name = raw_line.get("name", "")
            line_core_name = name_core(line_name)
            # 价格翻倍守卫：同核心词候选组价格稳健中位数，偏离 >2倍 的非赛特尔候选
            # 不给默认选中；赛特尔候选若远超全源稳健中位数（>3.5x）且组内有其他来源
            # 参照，同样不给默认选中（拦截 焦耳定律300 类版本差放大，不误伤 直尺22）。
            # 量程隔离：与询价量程明显冲突的候选（500mm直尺 vs 1000mm询价）不参与
            # 中位数统计——否则 22元 1000mm钢直尺 会被 4.5元 500mm直尺 拉出 3.5x 误杀。
            line_feat = _spec_features(f"{raw_line.get('name','')} {raw_line.get('spec','')}")
            line_spec_values = spec_values(f"{raw_line.get('name','')} {raw_line.get('spec','')}")

            def _same_range(item) -> bool:
                record_text = f"{item.record.get('name','')} {item.record.get('spec','')}"
                record_values = spec_values(record_text)
                for unit in ("mm", "ml", "g", "a", "v", "w"):
                    lv = line_spec_values.get(unit)
                    rv = record_values.get(unit)
                    # 按"任一数值吻合"判断量程是否同一档（修复 300mm 总长 vs φ6mm
                    # 直径被当成量程冲突，导致玻璃棒正确记录被排除在中位数之外）。
                    if lv and rv and not _values_overlap(lv, rv, 0.6):
                        return False
                return True

            group_prices = [
                float(item.record.get("price", 0))
                for item in candidates
                if item.record.get("price")
                and bigram_dice(line_core_name, name_core(item.record.get("name", ""))) >= MATCH_NAME_FLOOR
                and _same_range(item)
                # 核心词包含：电子天平 组只统计 电子天平，不混入 托盘天平/钩码 等
                # 仅 bigram 相似的产品——否则 14/30元 托盘天平 会把 385元 电子天平
                # 拉出 2x 守卫误杀，导致真实价被排除、走估算价兜底。
                and (
                    name_core(item.record.get("name", "")) in line_core_name
                    or line_core_name in name_core(item.record.get("name", ""))
                )
            ]
            group_median = robust_median(group_prices) if len(group_prices) >= 3 else 0.0
            non_saitel_prices = [
                float(item.record.get("price", 0))
                for item in candidates
                if item.record.get("price")
                and not is_saitel(item.record.get("source_file"))
                and bigram_dice(line_core_name, name_core(item.record.get("name", ""))) >= MATCH_NAME_FLOOR
                and _same_range(item)
                and (
                    name_core(item.record.get("name", "")) in line_core_name
                    or line_core_name in name_core(item.record.get("name", ""))
                )
            ]

            def _price_guard(item) -> bool:
                if not group_median:
                    return True
                price = float(item.record.get("price", 0))
                if is_saitel(item.record.get("source_file")):
                    if non_saitel_prices:
                        return price <= group_median * 3.5
                    return True
                return group_median / 2 <= price <= group_median * 2

            eligible = [
                item
                for item in candidates
                if item.score >= (SAITEL_FLOOR if is_saitel(item.record.get("source_file")) else AUTOSELECT_FLOOR)
                # 无价格条目（高中物理新课标等配备标准）只参与识别，不默认报价。
                and item.record.get("price")
                and name_allows_autoselect(line_name, item.record.get("name", ""), item.record.get("source_file"))
                and _price_guard(item)
            ]

            def _name_quality(item) -> int:
                record_core = name_core(item.record.get("name", ""))
                if not record_core:
                    return 0
                # 完全同名 2 分 > 包含关系 1 分（天文望远镜 280 应压过 望远镜 55——
                # "望远镜"是"天文望远镜"的后缀子串，但产品不同）。
                if record_core == line_core_name:
                    return 2
                return 1 if record_core in line_core_name else 0

            # 赛特尔内部优先级：老编号体系 sheet（初中物理/高中物理 等 5 位 JY 编码）
            # 高于 初中新课标物理 等新课标 sheet（30307 13 位分类代码）。
            # 浙江三和标准答案按老编号体系（21021=9元 等），同名单名多价时优先老体系。
            # 注意：高中通用技术/小学数学 等非物理学科 sheet 不享受优先——否则
            # 直尺5.0(通用技术,无spec) 会压过 直尺6.0(演示用1m塑料米尺)。
            # 物理学科 sheet（初中物理/高中物理）再优先于小学/其他学科——天文望远镜
            # 询价是初中物理档（280元），不能被 小学科学 的 160元 压过。
            def _legacy_sheet_priority(item) -> int:
                sheet = str(item.record.get("source_sheet") or "")
                if sheet in ("初中物理", "高中物理"):
                    return 0
                if sheet in ("初中化学", "初中生物", "初中地理", "初中数学",
                             "高中化学", "高中生物", "高中地理", "高中数学",
                             "小学数学", "小学科学"):
                    return 1
                return 2

            # 赛特尔优先：有赛特尔候选时仅默认选中赛特尔（报价1 为赛特尔），
            # 其余来源仍作为备选展示；赛特尔内部按名称匹配质量→学段→分数排序。
            # 变体守卫：赛特尔候选若带 规格变体/BLOCK 警告（如初中电源顶替高中
            # 电源），不默认选中；“规格量程”告警对同名候选放宽——同名多版本里
            # 的 220mm vs Φ60mm 之类不同维度数字比对常有噪声（分数已含罚分）。
            def _default_warning_block(item) -> bool:
                exact_name = _name_quality(item) == 2
                for warning in item.warnings:
                    text = str(warning)
                    if text.startswith("规格变体"):
                        return True
                    if text.startswith("BLOCK:"):
                        # 单位不可换算（玻璃棒：询价"个" vs 历史"千克"）：同名记录
                        # 规格/编号吻合时允许默认选中并转人工复核，避免整行退化到
                        # 规格不符的低价错配记录（64054 1.3 元 vs 正确 12 元）。
                        if exact_name and text.startswith("BLOCK: 单位不可直接换算"):
                            continue
                        return True
                    if text.startswith("规格量程") and not exact_name:
                        return True
                return False

            saitel_eligible = [
                item
                for item in eligible
                if is_saitel(item.record.get("source_file")) and not _default_warning_block(item)
            ]
            # 目录里存在“同名配备标准条目但无价格”时（如高中物理新课标），不用旧
            # 课标近似品顶替默认选中——留待人工/待复核，避免错配报价。
            priced_exact = [
                item
                for item in eligible
                if _name_quality(item) == 2 and float(item.record.get("price", 0) or 0) > 0
            ]
            top_candidate = candidates[0] if candidates else None
            suppress_defaults = bool(
                top_candidate is not None
                and _name_quality(top_candidate) == 2
                and not top_candidate.record.get("price")
                and is_saitel(top_candidate.record.get("source_file"))
                and not priced_exact
            )
            if suppress_defaults:
                line.warnings = list(line.warnings or []) + ["目录中同名配备标准条目无价格，需人工询价"]

            # 学段优先：清单整体学段（任务级投票）作为同名同价的偏好——高中清单
            # 不拿初中版（直联泵 340 vs 220）。编号命中不单独判学段：新标准编号
            # 跨学段共享，命中初中新课标不代表清单是初中。
            line_grade = job_grade

            def _grade_rank(item) -> int:
                if not line_grade:
                    return 0
                record_grade = grade_marker(
                    item.record.get("source_sheet") or item.record.get("name", "")
                )
                return 0 if record_grade == line_grade else 1

            def _spec_rank(item) -> int:
                # 同品名同分时，无规格说明的记录让位给有规格的（可核验性优先），
                # 兼容 价目本导出名带脏后缀/新老版本并存 的精确同名竞争（58 vs 22）。
                return 0 if str(item.record.get("spec") or "").strip() else 1

            _HARD_MISMATCH_PREFIXES = (
                "规格量程", "规格尺寸", "规格件数", "规格倍率", "规格倍数",
                "规格形状", "规格型号", "规格变体", "规格定位", "规格配置不符",
                "磁钢型号", "修饰词变体",
            )

            def _mismatch_rank(item) -> int:
                # 带硬规格冲突的候选排在吻合候选之后（分数已扣，但避免老体系
                # 优先级再次把它们提到前面）。
                return 1 if any(
                    str(warning).startswith(_HARD_MISMATCH_PREFIXES) for warning in item.warnings
                ) else 0

            def _catalog_rank(item) -> int:
                # 新配备标准 sheet（初中新课标物理/高中物理新课标…）用于识别与
                # 参数对照，同品名有老价目本记录时优先老价目本（用户口径：
                # 老课标优先——弹簧组 21006 应报 12 元而不是新课标 40 元）。
                sheet = str(item.record.get("source_sheet") or "")
                return 1 if "新课标" in sheet else 0

            def _variant_rank(item) -> int:
                # 定位/配置一致优先：询价"演示用"应匹配演示用记录（分子结构模型
                # 演示用 140 vs 初中用 55 同分并列），"学生分组用"匹配分组用，
                # "含支杆"匹配含支杆版本。
                line_flags = _spec_features(f"{raw_line.get('name','')} {raw_line.get('spec','')}")
                record_flags = _spec_features(
                    f"{item.record.get('name','')} {item.record.get('spec','')}"
                )
                for key in ("teaching", "grouped", "student", "branch"):
                    if line_flags.get(key) and record_flags.get(key):
                        return 0
                return 1

            default_pool = saitel_eligible if saitel_eligible else eligible
            # 名称质量 > 规格可核验性 > 老价目本 > 无硬规格冲突 > 定位一致 >
            # 学段一致 > 分数 > 老体系。老价目本优先于硬冲突：用户口径按老价目本
            # 报基准价（枕形导体 老 35 元 vs 新课标 40 元），规格差异走提示/复核。
            default_pool.sort(
                key=lambda item: (
                    -_name_quality(item),
                    _spec_rank(item),
                    _catalog_rank(item),
                    _mismatch_rank(item),
                    _variant_rank(item),
                    _grade_rank(item),
                    -item.score,
                    _legacy_sheet_priority(item),
                )
            )
            # 厂商固定顺序：在 default_pool 的质量排序之上，先按厂商首现顺序
            # 决定"选哪几家"进入默认方案——同一厂商内部仍按名称质量/规格/分数
            # 取最佳代表记录；未知厂商（无制造商记录）排在已知厂商之后。
            def _mfr_order(item) -> int:
                identity = normalize_text(
                    item.record.get("manufacturer") or item.record.get("brand")
                )
                return mfr_appearance.get(identity, len(mfr_appearance))

            mfr_ordered_pool = sorted(
                default_pool,
                key=lambda item: (
                    _mfr_order(item),
                    -_name_quality(item),
                    _spec_rank(item),
                    _catalog_rank(item),
                    _mismatch_rank(item),
                    _variant_rank(item),
                    _grade_rank(item),
                    -item.score,
                    _legacy_sheet_priority(item),
                ),
            )
            defaults = {item.record["id"] for item in distinct_manufacturer_options(mfr_ordered_pool, job.requested_option_count, min_score=SAITEL_FLOOR)}
            if suppress_defaults:
                defaults = set()
            # 默认方案按“同核心词×价位簇×厂商”去重：同品同价位且同一厂商（或厂商
            # 未知）只保留一个默认选中，避免 3 个同价赛特尔方案占满 TOP 区，把不同
            # 价位挤到后面；不同厂商即使同价也是独立方案——多厂商价目库里多家同价
            # 是常态，只有全部保留才能出多厂商报价。
            seen_level: dict[str, list[tuple[str, float]]] = {}

            def _keep_default(item) -> bool:
                key = name_core(item.record.get("name", "")) or "?"
                price = float(item.record.get("price", 0))
                identity = normalize_text(item.record.get("manufacturer") or item.record.get("brand"))
                levels = seen_level.setdefault(key, [])
                for prev_identity, prev in levels:
                    if abs(price - prev) / max(prev, 1e-9) > 0.30:
                        continue
                    if not identity or not prev_identity or identity == prev_identity:
                        return False
                levels.append((identity, price))
                return True

            defaults = {item.record["id"] for item in default_pool if item.record["id"] in defaults and _keep_default(item)}
            # Drop duplicate candidates: identical manufacturer/brand + price +
            # spec + model only ever produce one option card (the best-ranked).
            seen_option_keys: set[tuple] = set()
            rank = 0
            discount = float(customer.discount_percent if customer else 0)
            line_has_selected = False
            base_order = sorted(
                candidates,
                key=lambda item: (
                    item.record["id"] not in defaults,
                    # 名称质量 > 规格可核验性 > 老价目本 > 无硬规格冲突 >
                    # 定位一致 > 学段一致 > 分数 > 老体系
                    -_name_quality(item),
                    _spec_rank(item),
                    _catalog_rank(item),
                    _mismatch_rank(item),
                    _variant_rank(item),
                    _grade_rank(item),
                    -item.score,
                    _legacy_sheet_priority(item),
                ),
            )
            ordered_candidates = diversify_price_levels(base_order, defaults, line_core_name)
            line_options: list[QuoteOption] = []
            for candidate in ordered_candidates:
                record = candidate.record
                base_price = candidate.normalized_price or float(record["price"])
                config_delta, config_note = config_price_delta(raw_line, record)
                if config_delta:
                    base_price = round(base_price + config_delta, 2)
                final_price = round(base_price * (1 - discount / 100), 2)
                dedup_key = (
                    normalize_text(record.get("manufacturer") or record.get("brand")),
                    final_price,
                    normalize_text(record.get("spec")),
                    normalize_text(record.get("model")),
                )
                if dedup_key in seen_option_keys:
                    continue
                seen_option_keys.add(dedup_key)
                rank += 1
                option_warnings = list(candidate.warnings)
                if config_delta:
                    option_warnings.append(
                        f"配置加价：{config_note}（{float(record['price']):g}→{base_price:g}）"
                    )
                if not _price_guard(candidate) and group_median:
                    option_warnings.append(
                        f"价格异常：偏离同组中位数（¥{group_median:g}）超过2倍，需人工核对"
                    )
                if discount:
                    option_warnings.append(f"已应用客户折扣 {discount:g}% ，需核对毛利")
                if needs_margin_approval:
                    option_warnings.append(
                        f"BLOCK: VIP折扣需核对最低毛利线（{customer.minimum_margin_percent:g}%）"
                    )
                option = QuoteOption(
                    line_id=line.id,
                    history_quote_id=record["id"],
                    rank=rank,
                    score=candidate.score,
                    confidence=candidate.confidence,
                    component_scores=candidate.component_scores,
                    reasons=candidate.reasons,
                    warnings=option_warnings,
                    unit_status=candidate.unit_status,
                    normalized_price=candidate.normalized_price,
                    selected=record["id"] in defaults and candidate.score >= (SAITEL_FLOOR if is_saitel(record.get("source_file")) else AUTOSELECT_FLOOR),
                    final_price=final_price,
                    **_record_snapshot(record),
                )
                db.add(option)
                line_options.append(option)
                if record["id"] in defaults and candidate.score >= (SAITEL_FLOOR if is_saitel(record.get("source_file")) else AUTOSELECT_FLOOR):
                    line_has_selected = True
                    if config_delta and not any("配置加价" in str(w) for w in (line.warnings or [])):
                        line.warnings = list(line.warnings or []) + [
                            f"配置加价：{config_note}（{float(record['price']):g}→{base_price:g}）"
                        ]
            if not line_has_selected and group_prices:
                # 估算价兜底：无默认选中时按"同 JY/名称类目+同学段"的第 10 分位价
                # 生成保守估算方案（类目分位比核心词组更稳定，避免错配产品拖低
                # 分位价），待人工复核。
                line_cat = infer_category_from_name(raw_line.get("name", ""))
                line_marker = grade_marker(raw_line.get("name", ""))
                est_pool_prices: list[float] = []
                for hist in history:
                    if not hist.get("price"):
                        continue
                    if line_cat and infer_category_from_name(hist.get("name", "")) != line_cat:
                        continue
                    if line_marker and grade_marker(hist.get("name", "")) not in ("", line_marker):
                        continue
                    est_pool_prices.append(float(hist["price"]))
                if len(est_pool_prices) >= 5:
                    sorted_pool = sorted(est_pool_prices)
                    # 第 10 分位：比 P25 更保守，避免错配污染
                    est_base = sorted_pool[max(0, (len(sorted_pool) - 1) // 10)]
                    est_label = f"类目第10分位(共{len(est_pool_prices)}条)"
                else:
                    # 类目样本不足：退回原"同核心词组第 25 分位"
                    sorted_prices = sorted(group_prices)
                    est_base = sorted_prices[min(len(sorted_prices) - 1, len(sorted_prices) // 4)]
                    est_label = "同核心词组低位分位"
                est_price = round(est_base * (1 - discount / 100), 2)
                rank += 1
                est_option = QuoteOption(
                    line_id=line.id,
                    # 估算方案不关联具体历史记录：避免把随机记录（司南）的
                    # 规格/编码带进导出（曾出现"分子结构模型"参数=司南、单价=1.1）。
                    history_quote_id=None,
                    rank=rank,
                    score=0.0,
                    confidence="low",
                    component_scores={},
                    reasons=["估算"],
                    warnings=[f"估算价：按{est_label}（¥{est_base:g}）生成，需人工复核"],
                    unit_status="exact",
                    normalized_price=None,
                    # 估算仅供参考，不默认选中——避免垃圾价（1.1 元）写进报价单。
                    selected=False,
                    final_price=est_price,
                )
                db.add(est_option)
                line_options.append(est_option)
                warnings.append(f"估算价（参考）：按{est_label}生成，需人工复核")
            # 厂商固定顺序重排 rank：默认选中的方案按厂商在价目库中的首现顺序
            # 占据 rank 1..N（报价1=首现厂商，如 赛特尔→瑞仕达→德欧），未选中
            # 方案保持原质量排序紧随其后；库不变则导出列序完全可复现。
            def _option_mfr_order(opt: QuoteOption) -> int:
                identity = normalize_text(opt.record_manufacturer or opt.record_brand)
                return mfr_appearance.get(identity, len(mfr_appearance))

            selected_opts = sorted(
                (opt for opt in line_options if opt.selected),
                key=lambda opt: (_option_mfr_order(opt), opt.rank),
            )
            unselected_opts = sorted(
                (opt for opt in line_options if not opt.selected), key=lambda opt: opt.rank
            )
            for new_rank, opt in enumerate([*selected_opts, *unselected_opts], start=1):
                opt.rank = new_rank
            if index % 200 == 0:
                job.progress = min(95, 5 + int((index + 1) / len(parsed_lines) * 90))
                db.commit()
        job.total_lines = len(parsed_lines)
        job.matched_lines = matched
        job.review_lines = review
        job.unmatched_lines = unmatched
        job.confirmed_lines = 0
        job.progress = 100
        job.status = "review"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(QuoteJob, job_id)
        if job:
            job.status = "failed"
            job.error_message = str(exc)
            job.progress = 100
            db.commit()
    finally:
        db.close()
        if history_db is not None and history_db is not db:
            history_db.close()


def run_worker() -> None:
    seed_database()
    interval = float(os.getenv("WORKER_POLL_SECONDS", "1.5"))
    while True:
        db = database.SessionLocal()
        try:
            job = db.scalar(
                select(QuoteJob).where(QuoteJob.status == "queued").order_by(QuoteJob.created_at).limit(1)
            )
            job_id = job.id if job else None
            if job:
                job.status = "reserved"
                db.commit()
        finally:
            db.close()
        if job_id:
            process_job(job_id)
        else:
            time.sleep(interval)
