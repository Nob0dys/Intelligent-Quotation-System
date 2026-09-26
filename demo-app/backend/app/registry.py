"""数据库注册表：库的中文显示名、备注、标签与使用记录。

设计要点（见 docs/改造方案_数据模块合并与导出修复.md）：

* **独立控制库** ``data/_registry.db``，不参与业务库的热切换。业务库切换时
  注册表不受影响，因此可以稳定记录"最近使用"，也不会因为切库而丢失中文名。
* **键名分离**：``key`` 是 ASCII 文件名键（对应 ``data/{key}.db``），
  ``display_name`` 是任意中文显示名。重命名只改显示名，不动文件，因此不会
  牵动全局 engine、WAL 兄弟文件与已绑定该库的报价任务。
* **自动登记**：磁盘上手工放入的 ``*.db`` 会在列出时自动登记，显示名取文件名。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, Integer, JSON, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .database import DATA_DIR

REGISTRY_FILE = "_registry"
TRASH_DIR_NAME = "_trash"
BACKUP_DIR_NAME = "_backup"

logger = logging.getLogger("quote.registry")

# 内部库不参与库列表展示（_registry 是控制库，quote_clean 是历史临时库）。
RESERVED_STEMS = {REGISTRY_FILE, "quote_clean"}

# Windows 保留设备名，不能直接做文件名。
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryBase(DeclarativeBase):
    pass


class DatabaseEntry(RegistryBase):
    """一个受管价目库的元数据。"""

    __tablename__ = "database_registry"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), index=True)
    normalized_name: Mapped[str] = mapped_column(String(120), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_registry_engine = None
_RegistrySession = None


def _ensure_engine():
    global _registry_engine, _RegistrySession
    if _registry_engine is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{(DATA_DIR / f'{REGISTRY_FILE}.db').as_posix()}"
        _registry_engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
        _RegistrySession = sessionmaker(bind=_registry_engine, autoflush=False, expire_on_commit=False)
        RegistryBase.metadata.create_all(bind=_registry_engine)
    return _RegistrySession


@contextmanager
def registry_session():
    session_factory = _ensure_engine()
    session: Session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---- 名称与键 ----------------------------------------------------------------

def normalize_name(value: object) -> str:
    """名称归一化：全角转半角 + 小写 + 去空格与常见分隔符。

    ``赛特尔25年`` / ``赛特尔 25 年`` / ``SAITEL_25`` 会被判为同一个名字。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.strip().lower()
    return re.sub(r"[\s_\-·．.]+", "", text)


def slugify_key(display_name: str, taken: set[str]) -> str:
    """由显示名派生一个 ASCII 文件键；中文名会退化为带哈希的键。"""
    ascii_part = re.sub(r"[^A-Za-z0-9]+", "_", unicodedata.normalize("NFKC", display_name or ""))
    ascii_part = ascii_part.strip("_").lower()
    if len(ascii_part) >= 3:
        base = ascii_part[:32]
    else:
        digest = hashlib.sha1((display_name or "").encode("utf-8")).hexdigest()[:8]
        base = f"db_{digest}"
    if base in WINDOWS_RESERVED:
        base = f"db_{base}"
    if base not in taken:
        return base
    for index in range(2, 1000):
        candidate = f"{base}_{index}"
        if candidate not in taken:
            return candidate
    return f"{base}_{uuid.uuid4().hex[:6]}"


def validate_display_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if not name:
        raise ValueError("请先输入数据库名称")
    if len(name) > 40:
        raise ValueError("数据库名称不能超过 40 个字符")
    if re.search(r'[\\/:*?"<>|]', name):
        raise ValueError('数据库名称不能包含 \\ / : * ? " < > | 字符')
    return name


# ---- 文件路径 ----------------------------------------------------------------

def db_path(key: str) -> Path:
    """库文件路径。

    系统库按 ``DATABASE_URL`` 解析，而不是 ``DATA_DIR/{key}.db``：它可能不在
    数据目录下（测试会把 DATABASE_URL 指到临时目录），按真实位置走才能让
    存在性判断与统计不失真。
    """
    from .database import DATABASE_URL, system_db_key

    if key and key == system_db_key():
        return Path(DATABASE_URL.split("///", 1)[-1]).expanduser().resolve()
    return DATA_DIR / f"{key}.db"


def trash_dir() -> Path:
    path = DATA_DIR / TRASH_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_dir() -> Path:
    path = DATA_DIR / BACKUP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_siblings(path: Path) -> list[Path]:
    """SQLite 的 WAL/SHM 兄弟文件，移动或删除时必须一并处理。"""
    return [Path(f"{path}-wal"), Path(f"{path}-shm")]


def safe_unlink(path: Path) -> bool:
    """尽力删除文件，返回是否删成功。

    与 ``main.safe_unlink`` 同一套理由（那边有完整注释）：清理是收尾动作，删不掉
    只该记一条日志，不能让整个请求 500。这里单独实现是为了避免 registry ↔ main
    的循环导入。
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except BaseException as exc:  # noqa: BLE001 — 清理失败不允许影响调用方结果
        logger.warning("文件清理失败（不影响本次操作结果）：%s（%s: %s）", path, type(exc).__name__, exc)
        return False


def disk_keys() -> list[str]:
    """扫描数据目录下可被管理的库文件键（已排除内部目录与保留名）。"""
    if not DATA_DIR.exists():
        return []
    keys = []
    for path in sorted(DATA_DIR.glob("*.db")):
        if path.stem in RESERVED_STEMS:
            continue
        keys.append(path.stem)
    return keys


# ---- 统计 --------------------------------------------------------------------

# 缓存键是"新鲜度指纹"，不是单个 mtime —— 见 _stats_fingerprint 的说明。
_stats_cache: dict[str, tuple[tuple, dict]] = {}


def _stats_fingerprint(path: Path) -> tuple:
    """统计缓存的新鲜度指纹。

    只拿主库 mtime 当键是**不够的**：SQLite 开着 WAL，写入会落进 ``-wal`` 而主库
    文件本身不被改动，mtime 因此不变 → 缓存永远命中，界面上的条数/大小会一直停在
    旧值。实测过这个故障：库里已经有 1 个报价任务，库列表却长期显示"任务 0"、
    大小也停在旧值，直到进程重启才纠正。

    把 ``-wal`` 的 mtime 与大小一并算进来，任何写入都会让缓存自然失效，
    不需要在每个写入口手动 invalidate（那样迟早会漏）。
    """
    try:
        main = path.stat()
    except OSError:
        return (0.0, 0, 0.0, 0)
    try:
        wal = Path(f"{path}-wal").stat()
        return (main.st_mtime, main.st_size, wal.st_mtime, wal.st_size)
    except OSError:
        # 没有 WAL（未开 WAL 或已 checkpoint 并删除）时退化成主库指纹。
        return (main.st_mtime, main.st_size, 0.0, 0)


def db_stats(key: str) -> dict:
    """只读统计：文件大小、价目条数、任务数（按文件指纹缓存）。"""
    path = db_path(key)
    if not path.exists():
        return {"size_mb": 0.0, "history_count": 0, "job_count": 0}
    fingerprint = _stats_fingerprint(path)
    cached = _stats_cache.get(key)
    if cached and cached[0] == fingerprint:
        return cached[1]
    size_mb = round(path.stat().st_size / 1024 / 1024, 1)
    history = jobs = 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "history_quotes" in tables:
                history = conn.execute("SELECT COUNT(*) FROM history_quotes").fetchone()[0]
            if "quote_jobs" in tables:
                jobs = conn.execute("SELECT COUNT(*) FROM quote_jobs").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        history = jobs = 0
    stats = {"size_mb": size_mb, "history_count": history, "job_count": jobs}
    _stats_cache[key] = (fingerprint, stats)
    return stats


def invalidate_stats(key: str | None = None) -> None:
    if key is None:
        _stats_cache.clear()
    else:
        _stats_cache.pop(key, None)


# ---- 登记与查询 --------------------------------------------------------------

def scan_and_register(session: Session) -> None:
    """把磁盘上手工放入的库文件自动登记进来（显示名取文件名）。

    系统库例外：它的文件名键是 ASCII（``quote_saitel``），但那是给程序看的；
    界面上应该显示人能认的名字，所以单独给一个默认显示名（可用
    ``QUOTE_SYSTEM_DB_NAME`` 覆盖）。只影响"首次登记"那一次，之后改名以
    注册表里的为准。
    """
    from .database import system_db_key

    system_key = system_db_key()
    # 只给一个干净的名字：界面上各处还会自己补"（系统默认）"后缀，
    # 名字里再带一次就变成"赛特尔25年（系统默认库）（系统默认）"。
    system_name = os.getenv("QUOTE_SYSTEM_DB_NAME", "赛特尔25年")
    known = {row.key for row in session.scalars(select(DatabaseEntry)).all()}
    changed = False
    # 系统库必须始终在列表里，哪怕它的文件不在数据目录下（见 db_path）。
    candidates = disk_keys()
    if system_key and system_key not in candidates:
        candidates.append(system_key)
    for key in candidates:
        if key in known:
            continue
        display_name = system_name if key == system_key else key
        session.add(
            DatabaseEntry(
                key=key,
                display_name=display_name,
                normalized_name=normalize_name(display_name),
                note="系统默认库（程序固定读取，不可删除）" if key == system_key
                else "自动登记（磁盘上已存在的库文件）",
            )
        )
        changed = True
    if changed:
        session.flush()


def get_entry(session: Session, key: str) -> DatabaseEntry | None:
    return session.get(DatabaseEntry, key)


def find_by_name(session: Session, display_name: str, exclude_key: str | None = None) -> DatabaseEntry | None:
    target = normalize_name(display_name)
    for row in session.scalars(select(DatabaseEntry)).all():
        if row.key == exclude_key:
            continue
        if row.normalized_name == target:
            return row
    return None


def all_entries(session: Session, include_trashed: bool = False) -> list[DatabaseEntry]:
    query = select(DatabaseEntry)
    if not include_trashed:
        query = query.where(DatabaseEntry.status != "trashed")
    return list(session.scalars(query).all())


def recent_recency():
    """条目"新鲜度"：用过就取最近使用时间，没用过则退回创建时间。

    只用 ``last_used_at`` 排序时，新建但尚未使用过的库全部是 NULL，会一起掉到
    末尾并按名称字母序排列——于是"最近常用 5 个"退化成"名称前 5 个"，用户刚建好
    的库反而不出现。退回 ``created_at`` 后，新建的库也能排到前面。
    """
    return func.coalesce(DatabaseEntry.last_used_at, DatabaseEntry.created_at)


def _recency_stamp(entry: DatabaseEntry) -> float:
    """Python 侧的同一套新鲜度取值，供列表排序使用。"""
    stamp = entry.last_used_at or entry.created_at
    return stamp.timestamp() if stamp else 0.0


def recent_entries(session: Session, limit: int = 5) -> list[DatabaseEntry]:
    """最近常用：按新鲜度倒序，其次累计使用次数、名称。"""
    return list(
        session.scalars(
            select(DatabaseEntry)
            .where(DatabaseEntry.status == "active")
            .order_by(
                recent_recency().desc(),
                DatabaseEntry.use_count.desc(),
                DatabaseEntry.display_name.asc(),
            )
            .limit(limit)
        ).all()
    )


def _score(entry: DatabaseEntry, query: str) -> int:
    """文本相关度打分；使用频次不参与打分，只在同分时打破并列。"""
    if not query:
        return 0
    name = normalize_name(entry.display_name)
    key = entry.key.lower()
    tags = " ".join(normalize_name(tag) for tag in (entry.tags or []))
    note = normalize_name(entry.note)
    if name.startswith(query):
        return 100
    if query in name:
        return 60
    if key.startswith(query):
        return 40
    if query in key:
        return 40
    if query in tags:
        return 25
    if query in note:
        return 15
    return 0


def search_entries(session: Session, query: str = "", limit: int = 5) -> list[DatabaseEntry]:
    """搜索库：默认返回最近常用；有查询词时按相关度排序。"""
    normalized = normalize_name(query)
    if not normalized:
        return recent_entries(session, limit)
    scored = []
    for entry in session.scalars(select(DatabaseEntry).where(DatabaseEntry.status == "active")).all():
        value = _score(entry, normalized)
        if value:
            scored.append((value, entry))
    scored.sort(
        key=lambda item: (
            -item[0],  # 相关度优先
            -_recency_stamp(item[1]),  # 同分时较新的在前（未使用过的按创建时间）
            -item[1].use_count,
            item[1].display_name,
        )
    )
    return [entry for _, entry in scored[:limit]]


# ---- 写操作 ------------------------------------------------------------------

def create_entry(
    session: Session,
    display_name: str,
    note: str = "",
    tags: list[str] | None = None,
    created_by_id: int | None = None,
    preferred_key: str | None = None,
) -> DatabaseEntry:
    name = validate_display_name(display_name)
    if find_by_name(session, name):
        raise ValueError(f"数据库名称「{name}」已存在")
    taken = {row.key for row in session.scalars(select(DatabaseEntry)).all()} | set(disk_keys())
    key = preferred_key or slugify_key(name, taken)
    if key in taken:
        key = slugify_key(name, taken)
    entry = DatabaseEntry(
        key=key,
        display_name=name,
        normalized_name=normalize_name(name),
        note=(note or "").strip(),
        tags=list(tags or []),
        created_by_id=created_by_id,
    )
    session.add(entry)
    session.flush()
    return entry


def update_entry(
    session: Session,
    entry: DatabaseEntry,
    display_name: str | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
) -> DatabaseEntry:
    if display_name is not None:
        name = validate_display_name(display_name)
        conflict = find_by_name(session, name, exclude_key=entry.key)
        if conflict:
            raise ValueError(f"数据库名称「{name}」已存在")
        entry.display_name = name
        entry.normalized_name = normalize_name(name)
    if note is not None:
        entry.note = note.strip()
    if tags is not None:
        entry.tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    entry.updated_at = utcnow()
    session.flush()
    return entry


def touch_use(session: Session, key: str) -> None:
    """记录"一次使用"：设为当前库 / 被报价任务选中 / 导入价目本。"""
    entry = session.get(DatabaseEntry, key)
    if entry is None:
        return
    entry.last_used_at = utcnow()
    entry.use_count = int(entry.use_count or 0) + 1
    session.flush()


def trash_entry(session: Session, entry: DatabaseEntry) -> dict:
    """软删除：先备份，再把库文件（含 WAL/SHM）移入回收站。"""
    path = db_path(entry.key)
    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_dir() / f"{entry.key}-{stamp}.db"
        shutil.copy2(path, backup)
        target = trash_dir() / f"{entry.key}-{stamp}.db"
        shutil.move(str(path), str(target))
        for sibling in db_siblings(path):
            if sibling.exists():
                shutil.move(str(sibling), str(target.parent / sibling.name))
    entry.status = "trashed"
    entry.trashed_at = utcnow()
    entry.updated_at = utcnow()
    session.flush()
    invalidate_stats(entry.key)
    return {"backup": str(backup) if backup else ""}


def restore_entry(session: Session, entry: DatabaseEntry) -> None:
    candidates = sorted(trash_dir().glob(f"{entry.key}-*.db"))
    if candidates:
        target = db_path(entry.key)
        shutil.move(str(candidates[-1]), str(target))
        for sibling in candidates[-1].parent.glob(f"{candidates[-1].stem}.db-*"):
            shutil.move(str(sibling), str(DATA_DIR / sibling.name))
    entry.status = "active"
    entry.trashed_at = None
    entry.updated_at = utcnow()
    session.flush()
    invalidate_stats(entry.key)


def purge_entry(session: Session, entry: DatabaseEntry) -> None:
    for candidate in trash_dir().glob(f"{entry.key}-*.db"):
        safe_unlink(candidate)
        for sibling in candidate.parent.glob(f"{candidate.stem}.db-*"):
            safe_unlink(sibling)
    session.delete(entry)
    session.flush()
    invalidate_stats(entry.key)
