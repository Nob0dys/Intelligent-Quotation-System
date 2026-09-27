from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import String, delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from . import database as dbmod
from . import history_rows
from . import registry
from .database import DATA_DIR, get_db
from .excel_service import (
    PRICE_ALIASES,
    create_export,
    find_header,
    open_workbook,
    option_field,
    option_source,
    parse_history_workbook,
)
from .matching import normalize_text
from .models import (
    AuditEvent,
    AuthSession,
    Customer,
    CustomerRequirement,
    HistoryQuote,
    QuoteJob,
    QuoteLine,
    QuoteOption,
    User,
    ConfirmedPriceDraft,
)
from .security import (
    SESSION_COOKIE,
    create_session,
    get_current_user,
    hash_password,
    require_admin,
    token_hash,
    verify_password,
)
from .services import audit, bump_history_version, history_dedup_key, process_job, seed_database


# DATA_DIR 统一由 database.py 提供（两处各读一次环境变量容易不一致）。
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "80")) * 1024 * 1024
RUN_INLINE_JOBS = os.getenv("QUOTE_RUN_INLINE_JOBS", "true").lower() == "true"

logger = logging.getLogger("quote.api")

# 导入临时文件的保留时长。正常情况下每次导入结束就删掉了，这里的清扫只是兜底：
# 万一某次没删成（文件被占用、删除被环境策略拦截），残留会在下次导入前被收走，
# 免得 data/tmp 无限堆积。
IMPORT_TEMP_TTL_HOURS = float(os.getenv("QUOTE_IMPORT_TMP_TTL_HOURS", "6"))


def safe_unlink(path: Path) -> bool:
    """尽力删除文件，返回是否删成功。

    清理是收尾动作，**绝不能反过来把一次成功的业务操作判成失败**。删除失败的现实
    来源不少：Windows 上文件仍被别的进程占用、受限环境里删除被安全策略拦截（实测
    会直接抛 ``SystemExit``）。所以这里连 ``BaseException`` 一起兜住，失败只记日志、
    不冒泡——否则一个删不掉的临时文件就会把"导入成功"变成 HTTP 500。
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except BaseException as exc:  # noqa: BLE001 — 见上：清理失败不允许影响调用方结果
        logger.warning("文件清理失败（不影响本次操作结果）：%s（%s: %s）", path, type(exc).__name__, exc)
        return False


def sweep_stale_import_temps(temp_dir: Path) -> int:
    """顺手清掉上次没删掉的陈旧导入临时文件（尽力而为，失败即跳过）。"""
    cutoff = time.time() - IMPORT_TEMP_TTL_HOURS * 3600
    removed = 0
    try:
        candidates = list(temp_dir.glob("import-*"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                removed += 1 if safe_unlink(candidate) else 0
        except OSError:
            continue
    if removed:
        logger.info("已清理 %d 个陈旧的导入临时文件（%s）", removed, temp_dir)
    return removed

@asynccontextmanager
async def lifespan(_app: FastAPI):
    seed_database()
    yield


app = FastAPI(title="智能报价系统 API", version="1.0.0", lifespan=lifespan)
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginInput(BaseModel):
    username: str
    password: str


class RequirementInput(BaseModel):
    attribute_name: str
    operator: str = "contains"
    value: str
    unit: str = ""
    required: bool = True
    notes: str = ""


class CustomerInput(BaseModel):
    name: str
    customer_type: Literal["ordinary", "special", "vip"] = "ordinary"
    discount_percent: float = Field(default=0, ge=0, le=100)
    minimum_margin_percent: float = Field(default=0, ge=0, le=100)
    preferred_manufacturers: list[str] = Field(default_factory=list)
    notes: str = ""
    requirements: list[RequirementInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_customer_policy(self):
        if self.customer_type == "special" and not self.requirements:
            raise ValueError("特殊要求客户至少需要一条结构化要求")
        return self


class LineUpdate(BaseModel):
    selected_option_ids: list[int] = Field(min_length=1, max_length=5)
    final_prices: dict[str, float] = Field(default_factory=dict)
    manual_note: str = ""


class ConfirmInput(BaseModel):
    override_reason: str = ""


class CustomerUpdate(BaseModel):
    name: str | None = None
    customer_type: Literal["ordinary", "special", "vip"] | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    minimum_margin_percent: float | None = Field(default=None, ge=0, le=100)
    preferred_manufacturers: list[str] | None = None
    notes: str | None = None
    requirements: list[RequirementInput] | None = None


class JobRenameInput(BaseModel):
    display_name: str = ""
    tax_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class ChangePasswordInput(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class UserCreateInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6)
    display_name: str = ""
    role: Literal["admin", "quote"] = "quote"


class UserUpdateInput(BaseModel):
    password: str | None = Field(default=None, min_length=6)
    active: bool | None = None
    display_name: str | None = None
    role: Literal["admin", "quote"] | None = None


class HistoryQuoteInput(BaseModel):
    name: str = Field(min_length=1)
    spec: str = ""
    unit: str = ""
    manufacturer: str = ""
    price: float = Field(gt=0)
    brand: str = ""
    model: str = ""
    quote_date: str = ""
    source_file: str = "手工录入"


class ManualOptionInput(BaseModel):
    manufacturer: str = ""
    brand: str = ""
    model: str = ""
    spec: str = ""
    unit: str = ""
    price: float = Field(gt=0)
    history_quote_id: str | None = None


class BatchConfirmInput(BaseModel):
    scope: Literal["current", "filtered", "ids"] = "filtered"
    line_ids: list[int] = Field(default_factory=list)
    page: int = 1
    page_size: int = 50
    row_filter: str = "all"
    query: str = ""


def user_payload(user: User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}


def managed_user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "active": user.active,
        "created_at": user.created_at.isoformat(),
    }


def customer_payload(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "customer_type": customer.customer_type,
        "discount_percent": customer.discount_percent,
        "minimum_margin_percent": customer.minimum_margin_percent,
        "preferred_manufacturers": customer.preferred_manufacturers or [],
        "notes": customer.notes,
        "created_at": customer.created_at.isoformat(),
        "requirements": [
            {
                "id": item.id,
                "attribute_name": item.attribute_name,
                "operator": item.operator,
                "value": item.value,
                "unit": item.unit,
                "required": item.required,
                "notes": item.notes,
            }
            for item in customer.requirements
        ],
    }


def job_payload(job: QuoteJob) -> dict:
    return {
        "id": job.id,
        "file_name": job.file_name,
        "display_name": job.display_name,
        "status": job.status,
        "progress": job.progress,
        "requested_option_count": job.requested_option_count,
        "tax_rate": job.tax_rate,
        "database_key": job.database_key or "",
        "database_name": job.database_name_snapshot or "",
        "total_lines": job.total_lines,
        "matched_lines": job.matched_lines,
        "review_lines": job.review_lines,
        "unmatched_lines": job.unmatched_lines,
        "confirmed_lines": job.confirmed_lines,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "customer": customer_payload(job.customer) if job.customer else None,
        "created_by": user_payload(job.created_by),
    }


def option_identity(option: QuoteOption, price_override: float | None = None) -> tuple:
    """Duplicate-selection key: manufacturer/brand + price + spec + model.

    Works for history-linked, cross-database (snapshot-only) and manual
    (history-free) options."""
    maker = option_field(option, "manufacturer") or option_field(option, "brand")
    spec, model = option_field(option, "spec"), option_field(option, "model")
    price = price_override if price_override is not None else option.final_price
    return (
        normalize_text(maker) or f"unknown:{option.id}",
        round(price, 2),
        normalize_text(spec),
        normalize_text(model),
    )


def option_payload(option: QuoteOption, internal: bool = True) -> dict:
    """方案的价目信息，三级回退：匹配时落库的快照 → 关联的历史记录 → 手工方案。

    任务级绑定（B1）下价目库可能不是当前库，历史记录关联为空，只有快照可用。
    """
    history = option.history_quote
    has_snapshot = bool(
        option.record_source_file
        or option.record_name
        or option.record_manufacturer
        or option.record_brand
    )
    if has_snapshot:
        record = {
            "id": option.history_quote_id or f"snapshot:{option.id}",
            "name": option.record_name or "",
            "spec": option.record_spec or "",
            "model": option.record_model or "",
            "brand": option.record_brand or "",
            "manufacturer": option.record_manufacturer or "",
            "unit": option.record_unit or "",
            "price": option.record_price if option.record_price is not None else option.final_price,
            "quote_date": option.record_quote_date or "",
        }
        if internal:
            record.update(
                {
                    "source_file": option.record_source_file or "",
                    "source_sheet": option.record_source_sheet or "",
                    "source_row": option.record_source_row or 0,
                }
            )
    elif history is not None:
        record = {
            "id": history.id,
            "name": history.name,
            "spec": history.spec,
            "model": history.model,
            "brand": history.brand,
            "manufacturer": history.manufacturer,
            "unit": history.unit,
            "price": history.price,
            "quote_date": history.quote_date,
        }
        if internal:
            record.update(
                {
                    "source_file": history.source_file,
                    "source_sheet": history.source_sheet,
                    "source_row": history.source_row,
                }
            )
    else:
        record = {
            "id": f"manual:{option.id}",
            "name": "",
            "spec": option.manual_spec or "",
            "model": option.manual_model or "",
            "brand": option.manual_brand or "",
            "manufacturer": option.manual_manufacturer or "",
            "unit": option.manual_unit or "",
            "price": option.final_price,
            "quote_date": "",
        }
        if internal:
            record.update({"source_file": "手工方案", "source_sheet": "", "source_row": 0})
    return {
        "id": option.id,
        "rank": option.rank,
        "score": option.score,
        "confidence": option.confidence,
        "component_scores": option.component_scores,
        "reasons": option.reasons,
        "warnings": option.warnings,
        "unit_status": option.unit_status,
        "normalized_price": option.normalized_price,
        "selected": option.selected,
        "final_price": option.final_price,
        "manual_note": option.manual_note,
        "record": record,
    }


def line_payload(line: QuoteLine, include_options: bool = False) -> dict:
    selected = [item for item in line.options if item.selected]
    result = {
        "id": line.id,
        "source_row": line.source_row,
        "sheet_name": line.sheet_name,
        "name": line.name,
        "spec": line.spec,
        "product_code": line.product_code,
        "model": line.model,
        "brand": line.brand,
        "manufacturer": line.manufacturer,
        "unit": line.unit,
        "quantity": line.quantity,
        "pricing_quantity": line.pricing_quantity,
        "status": line.status,
        "confidence": line.confidence,
        "recommended_score": line.recommended_score,
        "warnings": line.warnings,
        "confirmed": line.confirmed,
        "selected_option_count": len(selected),
        "primary_option": option_payload(sorted(selected, key=lambda item: item.rank)[0]) if selected else None,
    }
    if include_options:
        result["options"] = [option_payload(item) for item in line.options]
    result["suggested_action"] = _suggest_action(line)
    return result


def _suggest_action(line: QuoteLine) -> dict:
    """基于行状态给出推荐的人工操作。前端可直接显示给用户。"""
    options = sorted(line.options, key=lambda item: item.rank)
    selected = [item for item in options if item.selected]
    blocking = any(
        str(warning).startswith("BLOCK:")
        for item in options for warning in (item.warnings or [])
    )
    estimated = any(
        any("估算价" in str(w) for w in (item.warnings or []))
        for item in selected
    )
    if line.confirmed:
        return {"action": "none", "label": "已确认", "reason": ""}
    if blocking:
        return {
            "action": "must_review",
            "label": "阻断复核",
            "reason": "存在 BLOCK 警告（需求/单位/毛利），不可自动确认",
        }
    if not options:
        return {"action": "manual_quote", "label": "手工建方案", "reason": "无任何候选"}
    if not selected:
        return {"action": "pick_one", "label": "需人工选择", "reason": "有候选但无默认选中"}
    top = options[0]
    runner_up = options[1] if len(options) > 1 else None
    if estimated:
        return {
            "action": "review_estimate",
            "label": "复核估算价",
            "reason": "系统按类目低位分位估算，建议人工确认或调整",
        }
    if (
        line.confidence == "high"
        and line.recommended_score >= 80
        and runner_up is None
        or (runner_up is not None and top.score - runner_up.score >= 15)
    ):
        return {
            "action": "auto_confirm_ok",
            "label": "可直接确认",
            "reason": f"高分({line.recommended_score:.0f})且无并列候选",
        }
    if line.confidence in ("low", "unreliable") or line.recommended_score < 55:
        return {
            "action": "review_carefully",
            "label": "低置信复核",
            "reason": f"匹配置信度低({line.confidence})，建议查竞品/手工询价",
        }
    return {
        "action": "confirm_or_pick",
        "label": "可确认或换方案",
        "reason": f"中等置信({line.confidence})，建议人工最终把关",
    }


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.scalar(select(func.count(User.id)))
    return {"status": "ok", "service": "quote-api"}


@app.post("/api/auth/login")
def login(payload: LoginInput, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session(db, user)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        max_age=int(os.getenv("SESSION_HOURS", "12")) * 3600,
        path="/",
    )
    return user_payload(user)


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_payload(user)


@app.post("/api/auth/logout")
def logout(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    quote_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    if quote_session:
        auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(quote_session)))
        if auth:
            db.delete(auth)
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "user": user.username}


@app.post("/api/auth/change-password")
def change_password(
    payload: ChangePasswordInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    user.password_hash = hash_password(payload.new_password)
    audit(db, user.id, "user.change_password", "user", user.id)
    db.commit()
    return {"ok": True}


@app.get("/api/users")
def list_users(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    users = db.scalars(select(User).order_by(User.id)).all()
    return [managed_user_payload(item) for item in users]


@app.post("/api/users", status_code=201)
def create_user(
    payload: UserCreateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = payload.username.strip()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    new_user = User(
        username=username,
        display_name=payload.display_name.strip() or username,
        role=payload.role,
        password_hash=hash_password(payload.password),
    )
    db.add(new_user)
    db.flush()
    audit(db, user.id, "user.create", "user", new_user.id, {"username": new_user.username, "role": new_user.role})
    db.commit()
    return managed_user_payload(new_user)


@app.patch("/api/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == user.id:
        if payload.active is False:
            raise HTTPException(status_code=400, detail="不能停用自己的账号")
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(status_code=400, detail="不能降低自己的管理员权限")
    changed: dict = {}
    if payload.password is not None:
        target.password_hash = hash_password(payload.password)
        changed["password"] = "reset"
    if payload.active is not None:
        target.active = payload.active
        changed["active"] = payload.active
    if payload.display_name is not None:
        target.display_name = payload.display_name.strip() or target.username
        changed["display_name"] = target.display_name
    if payload.role is not None:
        target.role = payload.role
        changed["role"] = payload.role
    audit(db, user.id, "user.update", "user", target.id, changed)
    db.commit()
    return managed_user_payload(target)


@app.get("/api/customers")
def list_customers(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    customers = db.scalars(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.active.is_(True)).order_by(Customer.created_at.desc())
    ).all()
    return [customer_payload(item) for item in customers]


@app.post("/api/customers")
def create_customer(
    payload: CustomerInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Customer).where(Customer.name == payload.name)):
        raise HTTPException(status_code=409, detail="客户名称已存在")
    customer = Customer(
        name=payload.name,
        customer_type=payload.customer_type,
        discount_percent=payload.discount_percent,
        minimum_margin_percent=payload.minimum_margin_percent,
        preferred_manufacturers=payload.preferred_manufacturers,
        notes=payload.notes,
    )
    db.add(customer)
    db.flush()
    for requirement in payload.requirements:
        db.add(CustomerRequirement(customer_id=customer.id, **requirement.model_dump()))
    audit(db, user.id, "customer.create", "customer", customer.id, {"name": customer.name})
    db.commit()
    db.refresh(customer)
    customer = db.scalar(select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer.id))
    return customer_payload(customer)


@app.patch("/api/customers/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.scalar(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer_id)
    )
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    new_type = payload.customer_type or customer.customer_type
    requirement_count = (
        len(payload.requirements) if payload.requirements is not None else len(customer.requirements)
    )
    if new_type == "special" and requirement_count == 0:
        raise HTTPException(status_code=400, detail="特殊要求客户至少需要一条结构化要求")
    if payload.name is not None and payload.name != customer.name:
        if db.scalar(select(Customer).where(Customer.name == payload.name)):
            raise HTTPException(status_code=409, detail="客户名称已存在")
        customer.name = payload.name
    if payload.customer_type is not None:
        customer.customer_type = payload.customer_type
    if payload.discount_percent is not None:
        customer.discount_percent = payload.discount_percent
    if payload.minimum_margin_percent is not None:
        customer.minimum_margin_percent = payload.minimum_margin_percent
    if payload.preferred_manufacturers is not None:
        customer.preferred_manufacturers = payload.preferred_manufacturers
    if payload.notes is not None:
        customer.notes = payload.notes
    if payload.requirements is not None:
        for item in list(customer.requirements):
            db.delete(item)
        db.flush()
        for requirement in payload.requirements:
            db.add(CustomerRequirement(customer_id=customer.id, **requirement.model_dump()))
    audit(db, user.id, "customer.update", "customer", customer.id, {"name": customer.name})
    db.commit()
    db.expire_all()
    customer = db.scalar(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer_id)
    )
    return customer_payload(customer)


@app.delete("/api/customers/{customer_id}")
def delete_customer(
    customer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    customer.active = False
    audit(db, user.id, "customer.delete", "customer", customer.id, {"name": customer.name})
    db.commit()
    return {"ok": True, "id": customer.id}


@app.post("/api/quote-jobs", status_code=202)
def create_quote_job(
    background_tasks: BackgroundTasks,
    customer_id: int | None = Form(None),
    requested_option_count: int = Form(3, ge=1, le=5),
    tax_rate: float = Form(0.10, ge=0.0, le=1.0),
    database_key: str | None = Form(None),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and (not customer or not customer.active):
        raise HTTPException(status_code=404, detail="客户不存在")
    # 任务级绑定（B1）：校验所选价目库可用，并记下当时的显示名快照。
    bound_key = (database_key or "").strip() or None
    bound_name = ""
    if bound_key:
        with registry.registry_session() as rdb:
            registry.scan_and_register(rdb)
            entry = registry.get_entry(rdb, bound_key)
            if entry is None or entry.status != "active":
                raise HTTPException(status_code=404, detail="所选价目库不存在或已停用")
            if not registry.db_path(bound_key).exists():
                raise HTTPException(status_code=400, detail="所选价目库文件缺失，请重新选择")
            bound_name = entry.display_name
    original_name = Path(file.filename or "quote.xlsx").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls", ".csv"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xlsm / .xls / .csv 文件")
    job_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{job_id}{suffix or '.xlsx'}"
    size = 0
    with destination.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                output.close()
                safe_unlink(destination)
                raise HTTPException(status_code=413, detail=f"文件不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
            output.write(chunk)
    job = QuoteJob(
        id=job_id,
        customer_id=customer.id if customer else None,
        created_by_id=user.id,
        file_name=original_name,
        source_file_path=str(destination),
        status="queued",
        requested_option_count=requested_option_count,
        tax_rate=tax_rate,
        database_key=bound_key,
        database_name_snapshot=bound_name,
    )
    db.add(job)
    audit(db, user.id, "job.create", "quote_job", job.id, {"file": original_name, "database_key": bound_key or ""})
    db.commit()
    if bound_key:
        with registry.registry_session() as rdb:
            registry.touch_use(rdb, bound_key)
    job = db.scalar(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .where(QuoteJob.id == job.id)
    )
    if RUN_INLINE_JOBS:
        background_tasks.add_task(process_job, job_id)
    return job_payload(job)


@app.get("/api/quote-jobs")
def list_jobs(
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    jobs = db.scalars(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .order_by(QuoteJob.created_at.desc())
        .limit(limit)
    ).all()
    return [job_payload(item) for item in jobs]


@app.get("/api/quote-jobs/{job_id}")
def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.scalar(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    return job_payload(job)


@app.patch("/api/quote-jobs/{job_id}")
def rename_job(
    job_id: str,
    payload: JobRenameInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    if payload.tax_rate is not None:
        job.tax_rate = payload.tax_rate
    job.display_name = payload.display_name.strip() or None
    audit(db, user.id, "job.rename", "quote_job", job.id, {"display_name": job.display_name or "", "tax_rate": job.tax_rate})
    db.commit()
    return {"id": job.id, "display_name": job.display_name, "tax_rate": job.tax_rate}


@app.delete("/api/quote-jobs/{job_id}")
def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    source_file_path = job.source_file_path
    file_name = job.file_name
    db.execute(
        delete(QuoteOption).where(
            QuoteOption.line_id.in_(select(QuoteLine.id).where(QuoteLine.job_id == job_id))
        )
    )
    db.execute(delete(QuoteLine).where(QuoteLine.job_id == job_id))
    db.delete(job)
    audit(db, user.id, "job.delete", "quote_job", job_id, {"file": file_name})
    db.commit()
    if source_file_path:
        # 任务已提交删除，源文件删不掉不该让接口报错（否则用户看到"删除失败"、
        # 实际任务已经没了）。
        safe_unlink(Path(source_file_path))
    shutil.rmtree(EXPORT_DIR / job_id, ignore_errors=True)
    return {"ok": True, "id": job_id}


def json_text_contains(column, phrase: str):
    """Match JSON text serialized by either SQLite or PostgreSQL."""
    rendered = func.cast(column, String)
    escaped = json.dumps(phrase, ensure_ascii=True)[1:-1]
    return or_(rendered.like(f"%{phrase}%"), rendered.like(f"%{escaped}%"))


def apply_line_filters(statement, job_id: str, row_filter: str, query: str):
    statement = statement.where(QuoteLine.job_id == job_id)
    if row_filter == "confirmed":
        statement = statement.where(QuoteLine.confirmed.is_(True))
    elif row_filter == "pending":
        statement = statement.where(QuoteLine.confirmed.is_(False))
    elif row_filter in ("review", "unmatched", "suggested"):
        statement = statement.where(QuoteLine.status == row_filter)
    elif row_filter == "unit_conflict":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "单位不可直接换算"))
    elif row_filter == "parameter_conflict":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "未满足客户要求"))
    elif row_filter == "price_anomaly":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "价格偏离"))
    elif row_filter == "score_tie":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "同分多价"))
    elif row_filter == "low_confidence":
        statement = statement.where(QuoteLine.confidence.in_(["low", "unreliable", "review"]))
    if query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                QuoteLine.name.ilike(pattern),
                QuoteLine.product_code.ilike(pattern),
                QuoteLine.model.ilike(pattern),
                func.cast(QuoteLine.source_row, String) == query.strip(),
            )
        )
    return statement


@app.get("/api/quote-jobs/{job_id}/lines")
def list_job_lines(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=25, le=100),
    row_filter: str = Query("all"),
    query: str = Query(""),
    sort: str = Query("source_row"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.get(QuoteJob, job_id):
        raise HTTPException(status_code=404, detail="报价任务不存在")
    count_statement = apply_line_filters(select(func.count(QuoteLine.id)), job_id, row_filter, query)
    total = int(db.scalar(count_statement) or 0)
    page_count = max(1, (total + page_size - 1) // page_size)
    actual_page = min(page, page_count)
    statement = apply_line_filters(
        select(QuoteLine).options(
            selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote)
        ),
        job_id,
        row_filter,
        query,
    )
    if sort == "score_asc":
        statement = statement.order_by(QuoteLine.recommended_score.asc(), QuoteLine.source_row)
    elif sort == "score_desc":
        statement = statement.order_by(QuoteLine.recommended_score.desc(), QuoteLine.source_row)
    else:
        statement = statement.order_by(QuoteLine.source_row)
    lines = db.scalars(
        statement.offset((actual_page - 1) * page_size).limit(page_size)
    ).unique().all()
    return {
        "items": [line_payload(item) for item in lines],
        "total": total,
        "page": actual_page,
        "page_size": page_size,
        "page_count": page_count,
    }


@app.get("/api/quote-lines/{line_id}")
def get_line(
    line_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    return line_payload(line, include_options=True)


@app.patch("/api/quote-lines/{line_id}")
def update_line(
    line_id: int,
    payload: LineUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    available = {item.id: item for item in line.options}
    if any(option_id not in available for option_id in payload.selected_option_ids):
        raise HTTPException(status_code=400, detail="包含无效候选方案")
    identity_keys: set[tuple] = set()
    for option_id in payload.selected_option_ids:
        option = available[option_id]
        # Only fully identical options (same manufacturer + price + spec +
        # model) conflict; same manufacturer with a different quote is allowed.
        key = option_identity(option, price_override=payload.final_prices.get(str(option.id)))
        if key in identity_keys:
            raise HTTPException(status_code=400, detail="不能重复选择完全相同的方案")
        identity_keys.add(key)
    for option in line.options:
        option.selected = option.id in payload.selected_option_ids
        price = payload.final_prices.get(str(option.id))
        if price is not None:
            if price <= 0:
                raise HTTPException(status_code=400, detail="报价必须大于0")
            option.final_price = round(price, 2)
        if option.selected:
            option.manual_note = payload.manual_note
    line.confirmed = False
    line.status = "review"
    audit(
        db,
        user.id,
        "line.options.update",
        "quote_line",
        line.id,
        {"selected": payload.selected_option_ids, "note": payload.manual_note},
    )
    db.commit()
    return line_payload(line, include_options=True)


@app.post("/api/quote-lines/{line_id}/options", status_code=201)
def create_manual_option(
    line_id: int,
    payload: ManualOptionInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine).options(selectinload(QuoteLine.options)).where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    history = db.get(HistoryQuote, payload.history_quote_id) if payload.history_quote_id else None
    if payload.history_quote_id and history is None:
        raise HTTPException(status_code=404, detail="历史报价记录不存在")
    if not history and not (payload.manufacturer.strip() or payload.brand.strip()):
        raise HTTPException(status_code=400, detail="手工方案至少填写制造商或品牌之一")
    max_rank = max((item.rank for item in line.options), default=0)
    option = QuoteOption(
        line_id=line.id,
        history_quote_id=history.id if history else None,
        rank=max_rank + 1,
        score=0,
        confidence="manual",
        component_scores={},
        reasons=[],
        warnings=[],
        unit_status="manual",
        normalized_price=None,
        selected=True,
        final_price=round(payload.price, 2),
        manual_brand=payload.brand.strip(),
        manual_manufacturer=payload.manufacturer.strip(),
        manual_model=payload.model.strip(),
        manual_spec=payload.spec.strip(),
        manual_unit=payload.unit.strip(),
    )
    db.add(option)
    line.confirmed = False
    if line.status == "confirmed":
        line.status = "review"
    db.flush()
    audit(
        db,
        user.id,
        "line.option.create",
        "quote_option",
        option.id,
        {
            "line_id": line.id,
            "manufacturer": option.manual_manufacturer,
            "history_quote_id": option.history_quote_id,
            "price": option.final_price,
        },
    )
    db.commit()
    return option_payload(option)


@app.delete("/api/quote-options/{option_id}")
def delete_manual_option(
    option_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    option = db.get(QuoteOption, option_id)
    if not option:
        raise HTTPException(status_code=404, detail="报价方案不存在")
    if option.history_quote_id is not None:
        raise HTTPException(status_code=400, detail="仅允许删除手工方案")
    line_id = option.line_id
    db.delete(option)
    audit(db, user.id, "line.option.delete", "quote_option", option_id, {"line_id": line_id})
    db.commit()
    return {"ok": True, "id": option_id}


def _record_confirmed_price_draft(
    db: Session,
    line: QuoteLine,
    user: User,
    suggested_price: float | None,
) -> ConfirmedPriceDraft | None:
    """When a line is confirmed (or re-confirmed after price override) write a
    draft price record into the confirmed_price_drafts queue. Admins approve via
    the /api/price-drafts endpoints to promote it into HistoryQuote."""
    selected = sorted((item for item in line.options if item.selected), key=lambda o: o.rank)
    if not selected:
        return None
    primary = selected[0]
    history = primary.history_quote
    confirmed_price = float(primary.final_price)
    if confirmed_price <= 0:
        return None
    # 避免重复回写：同一行同一价已存在草稿则复用
    existing = db.scalar(
        select(ConfirmedPriceDraft).where(
            ConfirmedPriceDraft.line_id == line.id,
            ConfirmedPriceDraft.confirmed_price == confirmed_price,
            ConfirmedPriceDraft.status.in_(["pending", "approved"]),
        )
    )
    if existing:
        return existing
    draft = ConfirmedPriceDraft(
        line_id=line.id,
        job_id=line.job_id,
        name=line.name,
        spec=history.spec if history else (primary.manual_spec or line.spec),
        model=history.model if history else (primary.manual_model or line.model),
        brand=history.brand if history else (primary.manual_brand or line.brand),
        manufacturer=history.manufacturer if history else (primary.manual_manufacturer or line.manufacturer),
        unit=history.unit if history else (primary.manual_unit or line.unit),
        product_code=history.product_code if history else line.product_code,
        confirmed_price=confirmed_price,
        suggested_price=suggested_price,
        source="人工确认" if user.role != "admin" else "管理员确认",
        status="pending",
    )
    db.add(draft)
    return draft


@app.post("/api/quote-lines/{line_id}/confirm")
def confirm_line(
    line_id: int,
    payload: ConfirmInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    selected = [item for item in line.options if item.selected]
    if not selected:
        raise HTTPException(status_code=400, detail="请至少选择一个制造商方案")
    line.confirmed = True
    line.status = "confirmed"
    line.confirmed_by_id = user.id
    line.confirmed_at = datetime.now(timezone.utc)
    line.override_reason = payload.override_reason.strip()
    # 记录 recommended_score 作为 suggested 价，供后续差异分析
    primary = sorted(selected, key=lambda o: o.rank)[0]
    _record_confirmed_price_draft(
        db, line, user,
        suggested_price=float(line.recommended_score) if line.recommended_score else None,
    )
    job = db.get(QuoteJob, line.job_id)
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job.id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, "line.confirm", "quote_line", line.id, {"override_reason": line.override_reason})
    db.commit()
    return line_payload(line, include_options=True)


@app.post("/api/quote-jobs/{job_id}/batch-confirm")
def batch_confirm(
    job_id: str,
    payload: BatchConfirmInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    statement = select(QuoteLine).options(selectinload(QuoteLine.options))
    if payload.scope == "ids":
        statement = statement.where(QuoteLine.job_id == job_id, QuoteLine.id.in_(payload.line_ids))
    else:
        statement = apply_line_filters(statement, job_id, payload.row_filter, payload.query)
        if payload.scope == "current":
            statement = statement.order_by(QuoteLine.source_row).offset(
                (payload.page - 1) * payload.page_size
            ).limit(payload.page_size)
    lines = db.scalars(statement).unique().all()
    confirmed = skipped = 0
    for line in lines:
        selected = [item for item in line.options if item.selected]
        blocking = any(
            str(warning).startswith("BLOCK:") for item in selected for warning in (item.warnings or [])
        )
        if (
            line.confirmed
            or line.confidence != "high"
            or line.recommended_score < 80
            or blocking
            or not selected
        ):
            skipped += 1
            continue
        line.confirmed = True
        line.status = "confirmed"
        line.confirmed_by_id = user.id
        line.confirmed_at = datetime.now(timezone.utc)
        confirmed += 1
        # 批量确认也走草稿回写（人工核对过才 batch_confirm，因此算可回写样本）
        _record_confirmed_price_draft(
            db, line, user,
            suggested_price=float(line.recommended_score) if line.recommended_score else None,
        )
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job_id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, "job.batch_confirm", "quote_job", job_id, {"confirmed": confirmed, "skipped": skipped, "scope": payload.scope})
    db.commit()
    return {"confirmed": confirmed, "skipped": skipped, "confirmed_lines": job.confirmed_lines}


@app.post("/api/quote-jobs/{job_id}/reprocess", status_code=202)
def reprocess_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    job.status = "queued"
    job.progress = 0
    job.error_message = ""
    audit(db, user.id, "job.reprocess", "quote_job", job_id)
    db.commit()
    if RUN_INLINE_JOBS:
        background_tasks.add_task(process_job, job_id)
    return {"id": job_id, "status": "queued"}


@app.post("/api/quote-jobs/{job_id}/export/{variant}")
def export_job(
    job_id: str,
    variant: Literal["internal", "customer"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.scalar(
        select(QuoteJob)
        .options(
            selectinload(QuoteJob.lines)
            .selectinload(QuoteLine.options)
            .selectinload(QuoteOption.history_quote)
        )
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    # 未确认行在导出中留空（客户版）/标记待复核（内部复核版），不再阻断导出。
    return _deliver_export(job, variant, user, db)


@app.post("/api/quote-jobs/{job_id}/auto-confirm-and-export/{variant}")
def auto_confirm_and_export(
    job_id: str,
    variant: Literal["internal", "customer"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """一键导出：有默认选中方案的行自动确认，其余行保持待复核并直接导出。"""
    job = db.scalar(
        select(QuoteJob)
        .options(
            selectinload(QuoteJob.lines)
            .selectinload(QuoteLine.options)
            .selectinload(QuoteOption.history_quote)
        )
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    if job.status in ("queued", "reserved", "matching"):
        raise HTTPException(status_code=400, detail="任务仍在匹配中，请稍后再试")
    for line in job.lines:
        if line.confirmed:
            continue
        selected = [item for item in line.options if item.selected]
        if not selected:
            continue
        primary = sorted(selected, key=lambda item: item.rank)[0]
        # 估算价方案（待复核）不自动确认
        estimate = any(
            "估算价" in str(warning)
            for option in selected
            for warning in (option.warnings or [])
        )
        # 低置信/带冲突告警的自动选中不静默确认——留在待复核清单由人工处理
        risky = any(
            str(warning).startswith(("规格变体", "规格量程", "BLOCK:"))
            for warning in (primary.warnings or [])
        )
        if not estimate and not risky and primary.final_price:
            line.confirmed = True
            line.status = "confirmed"
            line.confirmed_by_id = user.id
            line.confirmed_at = datetime.now(timezone.utc)
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job.id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, f"job.auto_confirm_export.{variant}", "quote_job", job.id, {"confirmed_lines": job.confirmed_lines})
    return _deliver_export(job, variant, user, db)


def _deliver_export(job, variant: str, user, db) -> FileResponse:
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", Path(job.file_name).stem)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "内部复核版" if variant == "internal" else "客户版"
    file_name = f"{stem}_{label}_{stamp}.xlsx"
    path = EXPORT_DIR / job.id / file_name
    create_export(job, variant, str(path), db=db)
    job.status = "exported"
    audit(db, user.id, f"job.export.{variant}", "quote_job", job.id, {"file": file_name})
    db.commit()
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=file_name,
    )


@app.get("/api/history/search")
def history_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = f"%{q.strip()}%"
    rows = db.scalars(
        select(HistoryQuote)
        .where(
            or_(
                HistoryQuote.name.ilike(pattern),
                HistoryQuote.spec.ilike(pattern),
                HistoryQuote.model.ilike(pattern),
                HistoryQuote.brand.ilike(pattern),
                HistoryQuote.manufacturer.ilike(pattern),
            )
        )
        .order_by(HistoryQuote.source_priority.desc(), HistoryQuote.source_row)
        .limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "spec": item.spec,
            "model": item.model,
            "brand": item.brand,
            "manufacturer": item.manufacturer,
            "unit": item.unit,
            "price": item.price,
            "quote_date": item.quote_date,
            "source": f"{item.source_file} · {item.source_sheet} · 第{item.source_row}行",
        }
        for item in rows
    ]


# ---- 价目表格化编辑：分页浏览 + 变更集保存 --------------------------------
# 数据库是唯一真源，Excel 只是进出载体。前端把网格上的改动整理成"新增 / 修改 /
# 删除"的变更集提交，服务端在**单事务**内逐行校验后落库——不做整表覆盖，
# 否则会把别人并发导入的行误删。
#
# 冲突一律"拒绝该行并回传位置"，不静默覆盖：调用方据此把用户送到出问题的那一行。

BULK_EDIT_MAX_ROWS = int(os.getenv("QUOTE_BULK_EDIT_MAX_ROWS", "2000"))
BACKUP_KEEP_PER_KEY = int(os.getenv("QUOTE_BACKUP_KEEP", "10"))

# 排序白名单：不在表里的 sort 值一律退回"导入自然顺序"。
ROW_SORT_FIELDS = {
    "name": HistoryQuote.name,
    "spec": HistoryQuote.spec,
    "model": HistoryQuote.model,
    "brand": HistoryQuote.brand,
    "manufacturer": HistoryQuote.manufacturer,
    "unit": HistoryQuote.unit,
    "product_code": HistoryQuote.product_code,
    "price": HistoryQuote.price,
    "quantity": HistoryQuote.quantity,
    "quote_date": HistoryQuote.quote_date,
    "source_file": HistoryQuote.source_file,
    "source_row": HistoryQuote.source_row,
    "data_quality": HistoryQuote.data_quality,
}

ROW_READONLY_FIELDS = (
    "id",
    "source_file",
    "source_sheet",
    "source_row",
    "source_priority",
    "data_quality",
    "revision",
)


def _open_history_session(database_key: str | None):
    """打开指定价目库的会话（未指定时用系统默认库）。

    非系统库不走 ``init_db``，没有别的地方会补表结构；老库缺
    ``revision`` 列时读取都会报 "no such column"，所以打开前先补迁移。
    """
    key = (database_key or "").strip() or dbmod.system_db_key()
    if not key:
        raise HTTPException(status_code=400, detail="无法确定要操作的数据库")
    path = registry.db_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="数据库文件不存在")
    if key != dbmod.system_db_key():
        dbmod.ensure_schema_for_path(path)
    return dbmod.session_for_key(key), key


def _row_location(source_file: str, source_sheet: str, source_row: int) -> str:
    """把溯源三件套拼成用户能认的位置串，用于冲突提示。"""
    parts = [str(source_file or "").strip() or "（无来源）"]
    if source_sheet:
        parts.append(str(source_sheet))
    if source_row:
        parts.append(f"第{source_row}行")
    return " · ".join(parts)


def _backup_history_db(key: str) -> Path:
    """用 SQLite 在线备份 API 拷一份库文件。

    直接 ``shutil.copy`` 只复制主文件，WAL 里未 checkpoint 的提交会丢；
    在线备份走连接读一致性快照，WAL 内容会被正确并入目标文件。
    """
    source_path = registry.db_path(key)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = registry.backup_dir() / f"{key}_{stamp}_{uuid.uuid4().hex[:6]}.db"
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    _prune_backups(key)
    return target


def _prune_backups(key: str, keep: int = BACKUP_KEEP_PER_KEY) -> None:
    """只保留最近若干份备份，避免 _backup 目录无限膨胀。"""
    files = sorted(
        registry.backup_dir().glob(f"{key}_*.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in files[keep:]:
        safe_unlink(stale)


class BulkEditRowInput(BaseModel):
    id: str | None = None
    revision: int | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class BulkEditInput(BaseModel):
    database_key: str | None = None
    created: list[BulkEditRowInput] = Field(default_factory=list)
    updated: list[BulkEditRowInput] = Field(default_factory=list)
    deleted: list[BulkEditRowInput] = Field(default_factory=list)
    backup: bool = True


@app.get("/api/history/rows")
def list_history_rows(
    database_key: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    q: str = Query(""),
    sort: str = Query("source"),
    order: str = Query("asc"),
    user: User = Depends(get_current_user),
):
    """价目库分页浏览。``/api/history/search`` 必须带关键词且上限 30 条，
    撑不起表格视图，因此单开一个可分页、可排序、可全量翻页的列表接口。"""
    session, key = _open_history_session(database_key)
    try:
        stmt = select(HistoryQuote)
        keyword = q.strip()
        if keyword:
            pattern = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    HistoryQuote.name.ilike(pattern),
                    HistoryQuote.spec.ilike(pattern),
                    HistoryQuote.model.ilike(pattern),
                    HistoryQuote.brand.ilike(pattern),
                    HistoryQuote.manufacturer.ilike(pattern),
                    HistoryQuote.product_code.ilike(pattern),
                    HistoryQuote.source_file.ilike(pattern),
                )
            )
        total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        column = ROW_SORT_FIELDS.get(sort)
        if column is not None:
            stmt = stmt.order_by(
                column.desc() if order == "desc" else column.asc(), HistoryQuote.id
            )
        else:
            stmt = stmt.order_by(
                HistoryQuote.source_file,
                HistoryQuote.source_sheet,
                HistoryQuote.source_row,
                HistoryQuote.id,
            )
        records = session.scalars(
            stmt.offset((page - 1) * page_size).limit(page_size)
        ).all()
        return {
            "database_key": key,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "editable_fields": list(history_rows.EDITABLE_FIELDS),
            "readonly_fields": list(ROW_READONLY_FIELDS),
            "max_bulk_rows": BULK_EDIT_MAX_ROWS,
            "rows": [history_rows.row_payload(item) for item in records],
        }
    finally:
        session.close()


@app.post("/api/history/bulk-edit")
def bulk_edit_history(
    payload: BulkEditInput,
    user: User = Depends(require_admin),
):
    """按变更集保存表格编辑结果。

    处理顺序：备份 → 建去重索引 → 删除 → 修改 → 新增 → 版本号 + 审计 → 提交。
    删除排在前面，用户"删掉旧行、同时新增修正行"这类操作不会自己撞自己。

    逐行返回状态，任何一行出问题都不影响其它行：
      * ``ok``        已写入（``changed`` 列出真正变化的字段）
      * ``conflict``  被拒绝，``reason`` 为 ``revision_mismatch``（别人改过）
                      或 ``duplicate_key``（与库里某行重复，``conflict`` 给出
                      该行的 id / 名称 / 来源位置，供前端点击跳转）
      * ``rejected``  被拒绝，``reason`` 为 ``invalid``（校验不过）或 ``missing``
    """
    total_rows = len(payload.created) + len(payload.updated) + len(payload.deleted)
    if total_rows == 0:
        return {
            "ok": True,
            "database_key": payload.database_key or dbmod.system_db_key(),
            "applied": {"created": 0, "updated": 0, "deleted": 0},
            "unchanged": 0,
            "skipped_duplicates": 0,
            "rejected": 0,
            "results": [],
            "backup": "",
        }
    if total_rows > BULK_EDIT_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多提交 {BULK_EDIT_MAX_ROWS} 行，本次 {total_rows} 行。请分批保存。",
        )

    session, key = _open_history_session(payload.database_key)
    try:
        # 1) 写前备份。备份失败就中止——宁可不让保存，也不能出现无法回退的改动。
        backup_path: Path | None = None
        if payload.backup:
            try:
                backup_path = _backup_history_db(key)
            except Exception as exc:  # noqa: BLE001 - 转成用户可读的 500
                raise HTTPException(
                    status_code=500, detail=f"自动备份失败，已中止保存：{exc}"
                ) from exc

        # 2) 去重索引。只取参与去重键的列 + 溯源三列 + 名称，比整表 ORM 对象轻。
        #    名称是给前端"点击跳转到冲突行"用的：冲突行可能不在当前页，
        #    前端靠名称搜索定位，再按 id 高亮。
        existing = session.execute(
            select(
                HistoryQuote.id,
                HistoryQuote.name,
                HistoryQuote.spec,
                HistoryQuote.unit,
                HistoryQuote.manufacturer,
                HistoryQuote.price,
                HistoryQuote.source_file,
                HistoryQuote.source_sheet,
                HistoryQuote.source_row,
            )
        ).all()
        index: dict[tuple, str] = {}
        row_info: dict[str, dict] = {}
        for row in existing:
            index.setdefault(
                history_dedup_key(row[1], row[2], row[3], row[4], row[5]), row[0]
            )
            row_info[row[0]] = {
                "id": row[0],
                "name": row[1],
                "location": _row_location(row[6], row[7], row[8]),
            }

        def conflict_target(row_id: str, **extra) -> dict:
            body = dict(row_info.get(row_id) or {"id": row_id, "name": "", "location": ""})
            body.update(extra)
            return body

        results: list[dict] = []
        applied = {"created": 0, "updated": 0, "deleted": 0}
        unchanged = rejected = skipped_duplicates = 0

        def reject(op: str, row_id, reason: str, message: str, extra: dict | None = None):
            body = {"op": op, "id": row_id, "status": "rejected", "reason": reason, "message": message}
            if extra:
                body.update(extra)
            results.append(body)

        def conflict(op: str, row_id, reason: str, message: str, extra: dict):
            body = {"op": op, "id": row_id, "status": "conflict", "reason": reason, "message": message}
            body.update(extra)
            results.append(body)

        # 3) 删除：先摘掉旧键，后续新增/修改才不会被待删行挡住。
        for item in payload.deleted:
            record = session.get(HistoryQuote, item.id) if item.id else None
            if record is None:
                rejected += 1
                reject("delete", item.id, "missing", "该行已不存在（可能已被他人删除）")
                continue
            actual = int(record.revision or 1)
            if item.revision is not None and actual != int(item.revision):
                conflict(
                    "delete",
                    item.id,
                    "revision_mismatch",
                    f"该行已被他人修改（当前版本 {actual}，你基于 {item.revision}），请刷新后重试",
                    {
                        "conflict": conflict_target(
                            item.id, actual_revision=actual, expected_revision=int(item.revision)
                        )
                    },
                )
                continue
            old_key = history_dedup_key(
                record.name, record.spec, record.unit, record.manufacturer, record.price
            )
            if index.get(old_key) == record.id:
                index.pop(old_key, None)
            row_info.pop(record.id, None)
            session.delete(record)
            applied["deleted"] += 1
            results.append({"op": "delete", "id": item.id, "status": "ok"})

        # 4) 修改
        for item in payload.updated:
            record = session.get(HistoryQuote, item.id) if item.id else None
            if record is None:
                rejected += 1
                reject("update", item.id, "missing", "该行已不存在（可能已被他人删除）")
                continue
            actual = int(record.revision or 1)
            if item.revision is not None and actual != int(item.revision):
                conflict(
                    "update",
                    item.id,
                    "revision_mismatch",
                    f"该行已被他人修改（当前版本 {actual}，你基于 {item.revision}），请刷新后重试",
                    {
                        "conflict": conflict_target(
                            item.id, actual_revision=actual, expected_revision=int(item.revision)
                        )
                    },
                )
                continue
            unknown = sorted(set(item.changes) - set(history_rows.EDITABLE_FIELDS))
            if unknown:
                rejected += 1
                reject("update", item.id, "invalid", f"包含不可编辑字段：{'、'.join(unknown)}")
                continue
            try:
                prospective = history_rows.normalize_fields(
                    {**history_rows.fields_of(record), **item.changes}
                )
                history_rows.validate(prospective)
            except history_rows.RowValidationError as exc:
                rejected += 1
                reject("update", item.id, "invalid", str(exc))
                continue
            new_key = history_dedup_key(
                prospective["name"],
                prospective["spec"],
                prospective["unit"],
                prospective["manufacturer"],
                prospective["price"],
            )
            old_key = history_dedup_key(
                record.name, record.spec, record.unit, record.manufacturer, record.price
            )
            holder = index.get(new_key)
            if holder is not None and holder != record.id:
                skipped_duplicates += 1
                conflict(
                    "update",
                    item.id,
                    "duplicate_key",
                    f"改后与库中已有条目重复：{row_info.get(holder, {}).get('location', holder)}",
                    {"conflict": conflict_target(holder)},
                )
                continue
            changed = history_rows.apply_fields(record, item.changes)
            if not changed:
                unchanged += 1
                results.append({"op": "update", "id": item.id, "status": "ok", "changed": [], "revision": actual})
                continue
            if index.get(old_key) == record.id:
                index.pop(old_key, None)
            index[new_key] = record.id
            applied["updated"] += 1
            results.append(
                {
                    "op": "update",
                    "id": item.id,
                    "status": "ok",
                    "changed": changed,
                    "revision": int(record.revision or 1),
                }
            )

        # 5) 新增
        for item in payload.created:
            unknown = sorted(set(item.changes) - set(history_rows.EDITABLE_FIELDS))
            if unknown:
                rejected += 1
                reject("create", item.id, "invalid", f"包含不可编辑字段：{'、'.join(unknown)}")
                continue
            try:
                values = history_rows.normalize_fields(item.changes)
                history_rows.validate(values)
            except history_rows.RowValidationError as exc:
                rejected += 1
                reject("create", item.id, "invalid", str(exc))
                continue
            new_key = history_dedup_key(
                values["name"], values["spec"], values["unit"], values["manufacturer"], values["price"]
            )
            holder = index.get(new_key)
            if holder is not None:
                skipped_duplicates += 1
                conflict(
                    "create",
                    item.id,
                    "duplicate_key",
                    f"与库中已有条目重复：{row_info.get(holder, {}).get('location', holder)}",
                    {"conflict": conflict_target(holder)},
                )
                continue
            record = history_rows.build_history_row(
                f"manual:{uuid.uuid4()}",
                source_file="表格编辑",
                source_sheet="",
                source_row=0,
                source_priority=0,
                **values,
            )
            session.add(record)
            index[new_key] = record.id
            row_info[record.id] = {
                "id": record.id,
                "name": record.name,
                "location": _row_location("表格编辑", "", 0),
            }
            applied["created"] += 1
            results.append({"op": "create", "id": record.id, "status": "ok", "revision": 1})

        # 6) 提交。匹配池每次报价都从库里现读（services.process_job），
        #    matching 的 lru_cache 又都是纯函数缓存，因此不需要额外失效动作；
        #    bump_history_version 保留为版本留痕。
        session.flush()
        bump_history_version(session)
        audit(
            session,
            user.id,
            "history.bulk_edit",
            "history_quote",
            key,
            {
                "created": applied["created"],
                "updated": applied["updated"],
                "deleted": applied["deleted"],
                "rejected": rejected,
                "skipped_duplicates": skipped_duplicates,
                "backup": backup_path.name if backup_path else "",
            },
        )
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    registry.invalidate_stats(key)
    with registry.registry_session() as rdb:
        registry.touch_use(rdb, key)

    return {
        "ok": True,
        "database_key": key,
        "applied": applied,
        "unchanged": unchanged,
        "skipped_duplicates": skipped_duplicates,
        "rejected": rejected,
        "results": results,
        "backup": backup_path.name if backup_path else "",
    }


# ---- 打开本地价目本：先解析比对，再决定要不要落库 --------------------------
# 这一步只**解析与比对**，不写库：先把"会发生什么"摆出来（新增多少行、更新
# 哪几行、哪些需要人工指定），用户确认后再复用 bulk-edit 落库——校验、乐观锁、
# 去重、备份、审计全在那边，这里不重复实现。

OPEN_FILE_MAX_ROWS = int(os.getenv("QUOTE_OPEN_FILE_MAX_ROWS", "5000"))
OPEN_FILE_CANDIDATE_LIMIT = 5

# 价目本解析器真正能提供的字段。数量与报价日期不在其中（解析器不返回），
# 因此打开文件不会覆盖库里已有的数量/报价日期，这两个字段在网格里手工维护。
FILE_FIELDS: tuple[str, ...] = (
    "name",
    "spec",
    "model",
    "brand",
    "manufacturer",
    "unit",
    "product_code",
    "price",
)

# 身份字段的中文名，用来把"按什么匹配上的"讲清楚给用户看。
_IDENTITY_LABELS = {"name": "名称", "spec": "参数", "unit": "单位", "manufacturer": "制造商"}


def _workbook_provided_fields(path: Path) -> dict[str, set[str]]:
    """每个 sheet 的表头实际提供了哪些字段。

    必须按**表头**判断，不能按"单元格是否为空"判断：「文件里有品牌列但这一行
    空着」表示用户想清空该字段；「文件根本没有品牌列」表示用户不关心、更新时
    应保留库里的原值。两者混淆会静默清空库里已有的品牌/制造商。
    """
    workbook = open_workbook(str(path))
    try:
        provided: dict[str, set[str]] = {}
        for ws in workbook.sheets:
            header = find_header(ws, {"price": PRICE_ALIASES})
            if not header:
                continue
            _, columns = header
            provided[ws.title] = {field for field, column in columns.items() if column}
        return provided
    finally:
        workbook.close()


def _read_history_upload_full(
    file: UploadFile,
) -> tuple[list[dict], dict[str, set[str]], str]:
    """解析价目本，同时取回每个 sheet 提供的字段集合（临时文件只落一次盘）。"""

    def handler(path: Path, name: str):
        return parse_history_workbook(str(path), name), _workbook_provided_fields(path), name

    return _with_history_upload(file, handler)


def _file_values(row: dict, provided: set[str]) -> dict:
    """取出文件真正提供的字段值（名称始终保留，否则这行没有意义）。"""
    values: dict[str, Any] = {"name": row.get("name") or ""}
    for field in FILE_FIELDS:
        if field == "name" or field not in provided:
            continue
        values[field] = row.get("price") if field == "price" else (row.get(field) or "")
    return values


def _merge_file_into_current(current: dict, file_values: dict) -> tuple[dict, list[str]]:
    """把文件提供的字段覆盖到库里现值上，返回 (合并后的完整值, 变化字段名)。"""
    proposed = dict(current)
    proposed.update(file_values)
    return proposed, history_rows.diff_fields(current, proposed)


@app.post("/api/history/open-file")
def open_history_file(
    file: UploadFile = File(...),
    database_key: str | None = Form(None),
    user: User = Depends(require_admin),
):
    """打开本地价目本，返回逐行建议动作。**不写库。**

    动作取值：

    * ``create``     库中没有同一器材 → 新增
    * ``update``     库中恰好一条同一器材且文件与它不同 → 修改（``changed`` 列出差异）
    * ``unchanged``  库中恰好一条且完全相同 → 无需改动
    * ``ambiguous``  库中多条同一器材 → 需人工指定改哪一条（``targets`` 给候选，
                      每个候选都已算好合并结果）
    * ``duplicate``  文件内前面已出现同一器材，或与前面某行指向库里同一条 → 只保留第一条
    * ``invalid``    该行没有有效价格（或所在 sheet 根本没有价格列）→ 不载入

    "同一器材"用不含价格的身份键（名称+参数+单位+制造商）判断，所以只改价格的行
    会被认成 ``update`` 而不是新增。**只按文件确实提供了的身份字段匹配**：文件
    没有制造商列时就退化成名称+参数+单位，匹配变松、更容易撞出 ``ambiguous``，
    但不会把"更新"误判成"新增"。实际用了哪几个字段在 ``match_fields`` /
    ``match_labels`` 里回传，前端要把它显示出来。
    """
    rows, provided_fields, original_name = _read_history_upload_full(file)
    if len(rows) > OPEN_FILE_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"文件共 {len(rows)} 行，超过单次 {OPEN_FILE_MAX_ROWS} 行上限，请拆分后分次打开。",
        )

    session, key = _open_history_session(database_key)
    try:
        scanned = session.execute(
            select(
                HistoryQuote.id,
                HistoryQuote.name,
                HistoryQuote.spec,
                HistoryQuote.unit,
                HistoryQuote.manufacturer,
                HistoryQuote.price,
                HistoryQuote.revision,
                HistoryQuote.source_file,
                HistoryQuote.source_sheet,
                HistoryQuote.source_row,
            )
        ).all()
        records: list[dict] = [
            {
                "id": item[0],
                "name": item[1] or "",
                "spec": item[2] or "",
                "unit": item[3] or "",
                "manufacturer": item[4] or "",
                "price": item[5],
                "revision": int(item[6] or 1),
                "location": _row_location(item[7], item[8], item[9]),
            }
            for item in scanned
        ]

        # 按"文件提供了哪些身份字段"建索引：文件没有制造商列时，库里那行算不出
        # 同长度的键，只能退化成名称+参数+单位来匹配。同一个文件里不同 sheet 的
        # 列可能不同，所以索引按字段组合缓存，用到哪套建哪套。
        index_cache: dict[tuple[str, ...], dict[tuple, list[dict]]] = {}

        def identity_index(fields: tuple[str, ...]) -> dict[tuple, list[dict]]:
            cached = index_cache.get(fields)
            if cached is None:
                cached = {}
                for record in records:
                    cached.setdefault(
                        history_rows.identity_projection(record, fields), []
                    ).append(record)
                index_cache[fields] = cached
            return cached

        # 第一遍：定动作、收集需要读完整行的目标 id。
        prepared: list[dict] = []
        needed_ids: set[str] = set()
        seen_identity: dict[tuple, int] = {}
        claimed_target: dict[str, int] = {}
        for seq, row in enumerate(rows, start=1):
            provided = provided_fields.get(row["sheet_name"], set()) & set(FILE_FIELDS)
            # 只用文件确实提供了的身份字段匹配。少一个字段匹配就更松，撞上多条
            # 会走 ambiguous 交给用户指定，绝不在这里猜。
            match_fields = tuple(
                field
                for field in history_rows.IDENTITY_FIELDS
                if field == "name" or field in provided
            )
            match_labels = [_IDENTITY_LABELS[field] for field in match_fields]
            entry: dict[str, Any] = {
                "seq": seq,
                "source": _row_location(original_name, row["sheet_name"], row["source_row"]),
                "action": "",
                "reason": "",
                "target": None,
                "targets": [],
                "warnings": [],
                "match_fields": list(match_fields),
                "match_labels": match_labels,
                "file_values": _file_values(row, provided),
            }
            price = row.get("price")
            if price is None or float(price) <= 0:
                entry["action"] = "invalid"
                entry["reason"] = (
                    "该 sheet 没有价格列，这一行不会载入"
                    if not row.get("has_price_column", True)
                    else "该行没有有效单价，不会载入"
                )
                prepared.append(entry)
                continue
            identity = (match_fields, history_rows.identity_projection(row, match_fields))
            if identity in seen_identity:
                entry["action"] = "duplicate"
                entry["reason"] = (
                    f"与文件中第 {seen_identity[identity]} 行是同一器材"
                    f"（{'/'.join(match_labels)}相同），只保留第一条"
                )
                prepared.append(entry)
                continue
            seen_identity[identity] = seq
            candidates = identity_index(match_fields).get(identity[1], [])
            if not candidates:
                entry["action"] = "create"
                entry["reason"] = f"库中没有{'/'.join(match_labels)}相同的器材，将新增"
            elif len(candidates) == 1:
                target = candidates[0]
                if target["id"] in claimed_target:
                    entry["action"] = "duplicate"
                    entry["target"] = target
                    entry["reason"] = (
                        f"与文件中第 {claimed_target[target['id']]} 行指向库里的同一条记录，只保留第一条"
                    )
                else:
                    claimed_target[target["id"]] = seq
                    entry["action"] = "update"
                    entry["target"] = target
                    needed_ids.add(target["id"])
            else:
                entry["action"] = "ambiguous"
                entry["targets"] = candidates[:OPEN_FILE_CANDIDATE_LIMIT]
                entry["reason"] = (
                    f"库中有 {len(candidates)} 条{'/'.join(match_labels)}相同的器材，"
                    "需指定要更新哪一条"
                )
                if len(candidates) > OPEN_FILE_CANDIDATE_LIMIT:
                    entry["warnings"].append(
                        f"仅列出前 {OPEN_FILE_CANDIDATE_LIMIT} 条候选，"
                        f"另有 {len(candidates) - OPEN_FILE_CANDIDATE_LIMIT} 条未显示"
                    )
                needed_ids.update(item["id"] for item in entry["targets"])
            prepared.append(entry)

        # 第二遍：读回需要的完整行，算出合并结果（只覆盖文件真正提供的字段）。
        current_by_id: dict[str, dict] = {}
        if needed_ids:
            for record in session.scalars(
                select(HistoryQuote).where(HistoryQuote.id.in_(sorted(needed_ids)))
            ).all():
                current_by_id[record.id] = history_rows.editable_payload(record)

        summary = {
            "create": 0, "update": 0, "unchanged": 0,
            "ambiguous": 0, "duplicate": 0, "invalid": 0,
        }
        results: list[dict] = []
        for entry in prepared:
            file_values = entry.pop("file_values")
            action = entry["action"]
            if action == "update":
                target = entry["target"]
                current = current_by_id.get(target["id"], {})
                values, changed = _merge_file_into_current(current, file_values)
                entry["current"] = current
                entry["values"] = values
                entry["changed"] = changed
                if not changed:
                    action = "unchanged"
                    entry["reason"] = "与库中该条目完全一致，无需改动"
                else:
                    entry["reason"] = (
                        f"按{'/'.join(entry['match_labels'])}匹配到 1 条，"
                        f"检测到 {len(changed)} 处差异"
                    )
            elif action == "ambiguous":
                # 这行本身也要带上文件里的值：前端得靠它显示"是哪件器材要指定"，
                # 否则只能显示一个行号，用户根本认不出该改哪一条。
                entry["values"] = file_values
                entry["changed"] = []
                for target in entry["targets"]:
                    current = current_by_id.get(target["id"], {})
                    values, changed = _merge_file_into_current(current, file_values)
                    target["current"] = current
                    target["values"] = values
                    target["changed"] = changed
            elif action == "create":
                entry["values"] = file_values
                entry["changed"] = list(file_values.keys())
            else:
                entry["values"] = file_values
                entry["changed"] = []
            entry["action"] = action
            summary[action] += 1
            results.append(entry)

        return {
            "database_key": key,
            "source": original_name,
            "total": len(results),
            "summary": summary,
            "max_bulk_rows": BULK_EDIT_MAX_ROWS,
            "rows": results,
        }
    finally:
        session.close()


@app.post("/api/history", status_code=201)
def create_history_quote(
    payload: HistoryQuoteInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    record = history_rows.build_history_row(
        f"manual:{uuid.uuid4()}",
        source_file=payload.source_file.strip() or "手工录入",
        source_sheet="",
        source_row=0,
        source_priority=0,
        name=payload.name,
        spec=payload.spec,
        model=payload.model,
        brand=payload.brand,
        manufacturer=payload.manufacturer,
        unit=payload.unit,
        product_code="",
        price=payload.price,
        quote_date=payload.quote_date,
    )
    db.add(record)
    audit(db, user.id, "history.create", "history_quote", record.id, {"name": record.name})
    bump_history_version(db)
    db.commit()
    return {
        "id": record.id,
        "name": record.name,
        "spec": record.spec,
        "model": record.model,
        "brand": record.brand,
        "manufacturer": record.manufacturer,
        "unit": record.unit,
        "price": record.price,
        "quote_date": record.quote_date,
        "source": record.source_file,
    }


@app.get("/api/price-drafts")
def list_price_drafts(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """管理员查看人工确认价草稿队列（等待审核后进入历史库）。"""
    rows = db.scalars(
        select(ConfirmedPriceDraft)
        .where(ConfirmedPriceDraft.status == status)
        .order_by(ConfirmedPriceDraft.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "line_id": item.line_id,
            "job_id": item.job_id,
            "name": item.name,
            "spec": item.spec,
            "model": item.model,
            "brand": item.brand,
            "manufacturer": item.manufacturer,
            "unit": item.unit,
            "product_code": item.product_code,
            "confirmed_price": item.confirmed_price,
            "suggested_price": item.suggested_price,
            "source": item.source,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        }
        for item in rows
    ]


class PriceDraftReviewInput(BaseModel):
    action: Literal["approve", "reject"]


@app.post("/api/price-drafts/{draft_id}/review")
def review_price_draft(
    draft_id: int,
    payload: PriceDraftReviewInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Approve: 把草稿行写入正式 HistoryQuote；Reject: 丢弃。"""
    draft = db.get(ConfirmedPriceDraft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status != "pending":
        return {"ok": False, "reason": f"已审核过: {draft.status}"}
    draft.reviewed_by_id = user.id
    draft.reviewed_at = datetime.now(timezone.utc)
    if payload.action == "reject":
        draft.status = "rejected"
        db.commit()
        return {"ok": True, "status": "rejected"}
    # 批准：写入历史库（去重靠 history_dedup_key，重复则跳过）
    draft.status = "approved"
    dedup_key = history_dedup_key(
        draft.name, draft.spec, draft.unit, draft.manufacturer, draft.confirmed_price
    )
    existing = db.scalars(select(HistoryQuote)).all()
    seen_keys = {
        history_dedup_key(item.name, item.spec, item.unit, item.manufacturer, item.price)
        for item in existing
    }
    if dedup_key in seen_keys:
        db.commit()
        return {"ok": True, "status": "approved", "history_quote_id": None, "note": "重复条目，已跳过写入"}
    new_id = f"draft:{draft.id}"
    db.add(
        history_rows.build_history_row(
            new_id,
            source_file=f"人工确认:{draft.source}",
            source_sheet="",
            source_row=draft.id,
            source_priority=2,  # 人工确认价优先于导入价
            name=draft.name,
            spec=draft.spec,
            model=draft.model,
            brand=draft.brand,
            manufacturer=draft.manufacturer,
            unit=draft.unit,
            product_code=draft.product_code,
            price=draft.confirmed_price,
            quote_date=datetime.now(timezone.utc).date().isoformat(),
        )
    )
    audit(db, user.id, "price_draft.approve", "price_draft", draft.id, {"history_quote_id": new_id})
    bump_history_version(db)
    db.commit()
    return {"ok": True, "status": "approved", "history_quote_id": new_id}


@app.get("/api/governance/summary")
def governance_summary(
    database_key: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """治理指标。``database_key`` 用于查看系统库以外的其它价目库。"""
    session = db
    owned = False
    if database_key and database_key != dbmod.system_db_key():
        if not registry.db_path(database_key).exists():
            raise HTTPException(status_code=404, detail="数据库不存在")
        session = dbmod.session_for_key(database_key)
        owned = True
    try:
        records = session.scalars(select(HistoryQuote)).all()
        by_name: dict[str, list[HistoryQuote]] = {}
        for record in records:
            by_name.setdefault(record.normalized_name, []).append(record)
        duplicate_groups = sum(len(items) > 1 for items in by_name.values())
        unit_conflict_groups = sum(
            len({normalize_text(item.unit) for item in items if item.unit}) > 1 for items in by_name.values()
        )
        price_conflict_groups = sum(len({item.price for item in items}) > 1 for items in by_name.values())
        missing_manufacturer = sum(not item.manufacturer for item in records)
        audit_count = int(session.scalar(select(func.count(AuditEvent.id))) or 0)
        return {
            "database_key": database_key or dbmod.system_db_key(),
            "record_count": len(records),
            "unique_name_count": len(by_name),
            "duplicate_name_groups": duplicate_groups,
            "unit_conflict_groups": unit_conflict_groups,
            "price_conflict_groups": price_conflict_groups,
            "missing_manufacturer_count": missing_manufacturer,
            "audit_event_count": audit_count,
            "matching_weights": {"参数": 45, "型号": 15, "名称": 15, "制造商/品牌": 10, "单位": 10, "数据质量": 5},
        }
    finally:
        if owned:
            session.close()


@app.post("/api/governance/dedup-history")
def dedup_history(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    records = db.scalars(select(HistoryQuote).order_by(HistoryQuote.created_at, HistoryQuote.id)).all()
    keep_by_key: dict[tuple, HistoryQuote] = {}
    removed = 0
    for record in records:
        key = history_dedup_key(record.name, record.spec, record.unit, record.manufacturer, record.price)
        keep = keep_by_key.get(key)
        if keep is None:
            keep_by_key[key] = record
            continue
        db.execute(
            update(QuoteOption)
            .where(QuoteOption.history_quote_id == record.id)
            .values(history_quote_id=keep.id)
        )
        db.delete(record)
        removed += 1
    bump_history_version(db)
    audit(db, user.id, "history.dedup", "history_quote", "all", {"removed": removed})
    db.commit()
    return {"removed": removed}


def _with_history_upload(file: UploadFile, handler):
    """把上传落到临时文件、交给 handler 解析、随后删除。

    临时文件在 finally 里删掉（导入路径与"打开文件"路径都不保留原始 Excel），
    所以需要解析两遍的调用方必须在 handler 内部一次做完。

    清理走 :func:`safe_unlink` 而不是直接 ``unlink``：删临时文件是收尾动作，删不掉
    只该留个警告，绝不能把已经解析成功的上传变成 HTTP 500（历史故障：受限环境下
    ``Path.unlink`` 被安全策略拦截并抛 ``SystemExit``，用户看到的是"上传失败"，
    但数据其实一条都没写进去）。
    """
    original_name = Path(file.filename or "history.xlsx").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls", ".csv"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xlsm / .xls / .csv 文件")
    temp_dir = DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale_import_temps(temp_dir)
    temp_path = temp_dir / f"import-{uuid.uuid4()}{suffix or '.xlsx'}"
    try:
        temp_path.write_bytes(file.file.read())
        return handler(temp_path, original_name)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析该文件，请确认是有效的 Excel 文件")
    finally:
        safe_unlink(temp_path)


def _read_history_upload(file: UploadFile) -> tuple[list[dict], str]:
    """把上传的价目本解析成行；解析失败时抛 HTTPException。"""
    return _with_history_upload(
        file, lambda path, name: (parse_history_workbook(str(path), name), name)
    )


def _insert_history_rows(db: Session, rows: list[dict], original_name: str, user_id: int) -> dict:
    """把价目本行写入给定会话所属的库；重复与无效行跳过并计数。"""
    seen_keys = {
        history_dedup_key(item.name, item.spec, item.unit, item.manufacturer, item.price)
        for item in db.scalars(select(HistoryQuote)).all()
    }
    inserted = skipped_duplicates = skipped_invalid = 0
    for row in rows:
        price = row["price"]
        if price is not None and price <= 0:
            skipped_invalid += 1
            continue
        if price is None and row.get("has_price_column", True):
            skipped_invalid += 1
            continue
        key = history_dedup_key(row["name"], row["spec"], row["unit"], row["manufacturer"], price)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        record_id = f"{original_name}:{row['sheet_name']}:{row['source_row']}"
        if row.get("vendor_group"):
            # 宽表一行拆多厂商记录：同一 source_row 用厂商组号区分主键。
            record_id = f"{record_id}#{row['vendor_group']}"
        if db.get(HistoryQuote, record_id):
            skipped_duplicates += 1
            continue
        product_code = str(row.get("product_code") or "")
        db.add(
            history_rows.build_history_row(
                record_id,
                source_file=original_name,
                source_sheet=row["sheet_name"],
                source_row=row["source_row"],
                source_priority=0,
                name=row["name"],
                spec=row["spec"],
                model=row["model"],
                brand=row["brand"],
                manufacturer=row["manufacturer"],
                unit=row["unit"],
                product_code=product_code,
                price=price if price is not None else 0.0,
                quote_date="",
            )
        )
        inserted += 1
    audit(
        db,
        user_id,
        "history.import",
        "history_quote",
        original_name,
        {"inserted": inserted, "skipped_duplicates": skipped_duplicates, "skipped_invalid": skipped_invalid},
    )
    bump_history_version(db)
    db.commit()
    return {
        "inserted": inserted,
        "skipped_duplicates": skipped_duplicates,
        "skipped_invalid": skipped_invalid,
    }


@app.post("/api/governance/import-history")
def import_history(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, original_name = _read_history_upload(file)
    result = _insert_history_rows(db, rows, original_name, user.id)
    _touch_system_database()
    return result


@app.get("/api/audit-events")
def audit_events(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    events = db.scalars(
        select(AuditEvent).options(selectinload(AuditEvent.user)).order_by(AuditEvent.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "action": item.action,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "detail": item.detail,
            "created_at": item.created_at.isoformat(),
            "user": user_payload(item.user),
        }
        for item in events
    ]


# ---- 数据资产：价目库管理（列表 / 搜索 / 新建 / 重命名 / 删除 / 导入）--------
# 库的中文名、备注、标签与使用记录存放在独立控制库 data/_registry.db
# （见 registry.py），与业务数据互不影响。
# **没有"激活库 / 切换库"**：系统库（database.system_db_key()）是常量，只是所有
# 未显式指定库的读取路径的默认值；换库的唯一途径是改配置重启。报价与库的关系是
# 任务级绑定（B1）：任务记录 database_key，匹配时从该库读价目本，并把价目快照
# 写入 QuoteOption，因此不依赖跨库外键，也不需要全局选库。

def _entry_payload(entry, system_key: str) -> dict:
    stats = registry.db_stats(entry.key)
    return {
        "key": entry.key,
        "name": entry.display_name,
        "note": entry.note or "",
        "tags": list(entry.tags or []),
        "status": entry.status,
        "exists": registry.db_path(entry.key).exists(),
        "size_mb": stats["size_mb"],
        "history_count": stats["history_count"],
        "job_count": stats["job_count"],
        "is_system": entry.key == system_key,
        "created_at": entry.created_at.isoformat() if entry.created_at else "",
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else "",
        "last_used_at": entry.last_used_at.isoformat() if entry.last_used_at else "",
        "use_count": int(entry.use_count or 0),
    }


def _ensure_database_schema(path: Path) -> None:
    """确保目标库文件存在且表结构齐全（不改变系统库）。

    除建表外还要跑轻量迁移：老库缺 ``revision`` 等新列时，表格编辑保存会
    直接报 "no such column"。库文件不走 ``init_db``，没有别的地方会补。
    """
    dbmod.ensure_schema_for_path(path)


def _touch_system_database() -> None:
    """记录"系统库被使用了一次"（导入价目本等场景）。"""
    key = dbmod.system_db_key()
    if not key:
        return
    with registry.registry_session() as rdb:
        registry.scan_and_register(rdb)
        registry.touch_use(rdb, key)


@app.get("/api/databases")
def list_databases(user: User = Depends(get_current_user)):
    """列出所有受管价目库；读操作对报价员开放，写操作需管理员。"""
    system = dbmod.system_db_key()
    with registry.registry_session() as rdb:
        registry.scan_and_register(rdb)
        entries = registry.all_entries(rdb, include_trashed=True)
    active = sorted((item for item in entries if item.status != "trashed"), key=lambda item: item.display_name)
    trashed = sorted((item for item in entries if item.status == "trashed"), key=lambda item: item.display_name)
    return {
        "system": system,
        "databases": [_entry_payload(item, system) for item in active],
        "trashed": [_entry_payload(item, system) for item in trashed],
    }


@app.get("/api/databases/search")
def search_databases(
    q: str = Query("", max_length=80),
    scope: Literal["recent", "all"] = Query("recent"),
    limit: int = Query(5, ge=1, le=50),
    user: User = Depends(get_current_user),
):
    """数据库搜索：无关键词时返回"最近常用"（默认 5 个）。"""
    system = dbmod.system_db_key()
    with registry.registry_session() as rdb:
        registry.scan_and_register(rdb)
        if q.strip():
            entries = registry.search_entries(rdb, q, limit)
        elif scope == "all":
            entries = registry.recent_entries(rdb, limit)
        else:
            entries = registry.recent_entries(rdb, min(limit, 5))
        return {"system": system, "databases": [_entry_payload(item, system) for item in entries]}


class DatabaseCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    note: str = Field("", max_length=500)
    tags: list[str] = Field(default_factory=list)


class DatabaseUpdateInput(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=40)
    note: str | None = Field(None, max_length=500)
    tags: list[str] | None = None


class DatabasePurgeInput(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)


@app.post("/api/databases", status_code=201)
def create_database(
    payload: DatabaseCreateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """新建空库：登记元数据 + 建表。只是多了一个可选价目源，不影响任何全局状态。"""
    try:
        with registry.registry_session() as rdb:
            registry.scan_and_register(rdb)
            entry = registry.create_entry(
                rdb, payload.name, payload.note, payload.tags, created_by_id=user.id
            )
            db_key = entry.key
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _ensure_database_schema(registry.db_path(db_key))
    registry.invalidate_stats(db_key)
    audit(db, user.id, "database.create", "database", db_key, {"name": payload.name})
    db.commit()
    with registry.registry_session() as rdb:
        entry = registry.get_entry(rdb, db_key)
        body = _entry_payload(entry, dbmod.system_db_key())
    return {"ok": True, "database": body}


@app.patch("/api/databases/{db_key}")
def update_database(
    db_key: str,
    payload: DatabaseUpdateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """重命名 / 编辑：只改显示名与元数据，不动库文件。"""
    with registry.registry_session() as rdb:
        entry = registry.get_entry(rdb, db_key)
        if entry is None:
            raise HTTPException(status_code=404, detail="数据库不存在")
        try:
            registry.update_entry(rdb, entry, payload.name, payload.note, payload.tags)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        body = _entry_payload(entry, dbmod.system_db_key())
    if payload.name is not None:
        # 同步系统库中绑定该价目库的任务显示名。系统库是唯一的业务库，因此
        # 这里一次 UPDATE 就能覆盖全部任务，不存在"等某个库被激活"的滞后。
        db.execute(
            update(QuoteJob)
            .where(QuoteJob.database_key == db_key)
            .values(database_name_snapshot=body["name"])
        )
        audit(db, user.id, "database.rename", "database", db_key, {"name": body["name"]})
        db.commit()
    return {"ok": True, "database": body}


@app.delete("/api/databases/{db_key}")
def delete_database(
    db_key: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """软删除：先自动备份，再移入回收站，可恢复。"""
    if db_key == dbmod.system_db_key():
        raise HTTPException(status_code=400, detail="系统默认库不能删除")
    with registry.registry_session() as rdb:
        entry = registry.get_entry(rdb, db_key)
        if entry is None or entry.status == "trashed":
            raise HTTPException(status_code=404, detail="数据库不存在")
        display_name = entry.display_name
        bound_jobs = int(
            db.scalar(select(func.count(QuoteJob.id)).where(QuoteJob.database_key == db_key)) or 0
        )
        # Windows 下文件被进程持有就移不动，先释放该库的独立引擎。
        dbmod.dispose_key(db_key)
        result = registry.trash_entry(rdb, entry)
    audit(db, user.id, "database.delete", "database", db_key, {"name": display_name, "bound_jobs": bound_jobs})
    db.commit()
    return {"ok": True, "bound_jobs": bound_jobs, "backup": result["backup"]}


@app.post("/api/databases/{db_key}/restore")
def restore_database(
    db_key: str, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    with registry.registry_session() as rdb:
        entry = registry.get_entry(rdb, db_key)
        if entry is None or entry.status != "trashed":
            raise HTTPException(status_code=404, detail="回收站中没有该数据库")
        registry.restore_entry(rdb, entry)
        body = _entry_payload(entry, dbmod.system_db_key())
    audit(db, user.id, "database.restore", "database", db_key, {"name": body["name"]})
    db.commit()
    return {"ok": True, "database": body}


@app.post("/api/databases/{db_key}/purge")
def purge_database(
    db_key: str,
    payload: DatabasePurgeInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """彻底删除（不可恢复）：必须回填库名确认。"""
    with registry.registry_session() as rdb:
        entry = registry.get_entry(rdb, db_key)
        if entry is None or entry.status != "trashed":
            raise HTTPException(status_code=404, detail="回收站中没有该数据库")
        if registry.normalize_name(payload.confirm_name) != registry.normalize_name(entry.display_name):
            raise HTTPException(status_code=400, detail="输入的数据库名称不匹配")
        display_name = entry.display_name
        dbmod.dispose_key(db_key)
        registry.purge_entry(rdb, entry)
    audit(db, user.id, "database.purge", "database", db_key, {"name": display_name})
    db.commit()
    return {"ok": True}


@app.post("/api/databases/{db_key}/import")
def import_into_database(
    db_key: str,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
):
    """把价目本导入指定库。"""
    with registry.registry_session() as rdb:
        registry.scan_and_register(rdb)
        entry = registry.get_entry(rdb, db_key)
        if entry is None or entry.status != "active":
            raise HTTPException(status_code=404, detail="数据库不存在")
        display_name = entry.display_name
    _ensure_database_schema(registry.db_path(db_key))
    rows, original_name = _read_history_upload(file)
    session = dbmod.session_for_key(db_key)
    try:
        result = _insert_history_rows(session, rows, original_name, user.id)
    finally:
        session.close()
    registry.invalidate_stats(db_key)
    with registry.registry_session() as rdb:
        registry.touch_use(rdb, db_key)
    return {**result, "database": display_name}
