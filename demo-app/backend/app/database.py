from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable


class Base(DeclarativeBase):
    pass


# 交付包根目录（app/ → backend/ → demo-app/ → 包根）。
# 数据目录默认挂在包根，而不是按 CWD 解析的 ./data：双击 启动.bat（CWD=backend）
# 和手动 cd backend 起服务读到的必须是同一份数据，早先两者会指向不同的库。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def sqlite_url(path: Path) -> str:
    """把本地路径转成 SQLAlchemy 的 sqlite URL（Windows 盘符同样适用）。"""
    return "sqlite:///" + Path(path).resolve().as_posix()


# 数据目录（业务库、上传件、导出件、控制库都在这里）。
DATA_DIR = Path(os.getenv("QUOTE_DATA_DIR") or (_PROJECT_ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 系统库：既是唯一的默认价目库，也是用户 / 客户 / 任务 / 审计的存放处。
# 它是**常量**——系统里没有"激活库 / 切换库"这回事了，要换库只能改这个键再重启。
SYSTEM_DB_KEY = os.getenv("QUOTE_SYSTEM_DB", "quote_saitel")
DATABASE_URL = os.getenv("DATABASE_URL") or sqlite_url(DATA_DIR / f"{SYSTEM_DB_KEY}.db")


def system_db_key() -> str:
    """系统库的键（文件名去后缀）；非 sqlite 时返回空串。

    这是"默认库"的唯一来源。所有未显式指定 ``database_key`` 的读取路径都落到
    它上面；它自己不可删除、也不可被切换。
    """
    if not DATABASE_URL.startswith("sqlite"):
        return ""
    raw = DATABASE_URL.split("///", 1)[-1]
    if ":memory:" in raw:
        return ""
    return Path(raw).expanduser().resolve().stem


if DATABASE_URL.startswith("sqlite"):
    if ":memory:" not in DATABASE_URL:
        db_path = DATABASE_URL.split("///", 1)[-1]
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    # 保持 SQLAlchemy 默认事务管理（isolation_level 默认），PRAGMA 通过
    # connect 事件在每个新连接上设置——WAL+NORMAL 提升批量写入性能，
    # 同时不破坏事务批处理（否则每条 INSERT 独立提交反而更慢）。
    connect_args = {"check_same_thread": False, "timeout": 30}
    engine_kwargs = {"connect_args": connect_args}
    if ":memory:" in DATABASE_URL:
        engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """连接级 PRAGMA：WAL 日志 + NORMAL 同步 + 大缓存（批量写入性能关键）。
    通过 connect 事件对每个新建连接生效，不改变 SQLAlchemy 事务语义。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-8192")
    finally:
        cursor.close()


if DATABASE_URL.startswith("sqlite") and ":memory:" not in DATABASE_URL:
    from sqlalchemy import event as _sqlalchemy_event

    _sqlalchemy_event.listen(engine, "connect", _set_sqlite_pragmas)


# 任务级绑定（B1）：读取非系统库的价目本时使用的独立引擎缓存。
# 这些引擎不参与任何全局状态，因此多个库可以同时被不同任务读取。
_extra_sessionmakers: dict[str, sessionmaker] = {}


def session_for_key(db_key: str):
    """返回一个绑定到 ``DATA_DIR/{db_key}.db`` 的 Session。

    ``db_key`` 等于系统库时直接复用全局 SessionLocal；否则按需建一个独立引擎
    （缓存复用），不改变任何全局状态——这是任务级绑定不互相干扰的关键。
    """
    if not db_key or db_key == system_db_key():
        return SessionLocal()
    factory = _extra_sessionmakers.get(db_key)
    if factory is None:
        target = DATA_DIR / f"{db_key}.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        extra_engine = create_engine(
            sqlite_url(target), connect_args={"check_same_thread": False, "timeout": 30}
        )
        from sqlalchemy import event as _event

        _event.listen(extra_engine, "connect", _set_sqlite_pragmas)
        factory = sessionmaker(bind=extra_engine, autoflush=False, expire_on_commit=False)
        _extra_sessionmakers[db_key] = factory
    return factory()


def dispose_key(db_key: str) -> None:
    """释放某个库的独立引擎（删除/移动库文件前必须调用，否则 Windows 下删不掉）。"""
    factory = _extra_sessionmakers.pop(db_key, None)
    if factory is not None:
        factory.kw["bind"].dispose()



# Columns added to quote_options for manual (history-free) quote options.
MANUAL_OPTION_COLUMNS = {
    "manual_brand": "VARCHAR(300)",
    "manual_manufacturer": "VARCHAR(500)",
    "manual_model": "VARCHAR(300)",
    "manual_spec": "TEXT",
    "manual_unit": "VARCHAR(80)",
}

# 任务级绑定（B1）：任务记录它使用的价目库。
JOB_BINDING_COLUMNS = {
    "database_key": "VARCHAR(80)",
    "database_name_snapshot": "VARCHAR(120)",
}

# 匹配时落库的价目快照。跨库报价时历史记录不在当前库中，外键关联为空，
# 导出/展示只能依赖这份快照，因此每个方案的描述字段都要冗余一份。
OPTION_RECORD_COLUMNS = {
    "record_source_file": "VARCHAR(300)",
    "record_source_sheet": "VARCHAR(200)",
    "record_source_row": "INTEGER",
    "record_name": "VARCHAR(500)",
    "record_spec": "TEXT",
    "record_model": "VARCHAR(300)",
    "record_brand": "VARCHAR(300)",
    "record_manufacturer": "VARCHAR(500)",
    "record_unit": "VARCHAR(80)",
    "record_product_code": "VARCHAR(200)",
    "record_price": "FLOAT",
    "record_quote_date": "VARCHAR(40)",
}

# 表格化编辑需要的乐观锁列：提交时带上读取时的 revision，不一致即判冲突。
HISTORY_ROW_COLUMNS = {
    "revision": "INTEGER DEFAULT 1",
}


def _is_sqlite(target_engine) -> bool:
    return target_engine.dialect.name == "sqlite"


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
    _apply_round3_migrations()


def ensure_schema_for_path(path: Path) -> None:
    """给非系统库的 .db 文件补齐表结构 + 跑轻量迁移。

    任务级绑定（B1）允许读任意价目库，表格编辑还允许**改**任意价目库。
    这些库不走 ``init_db``，因此不会自动建表、也不会跑迁移——老库缺
    ``revision`` 列时保存会直接报 "no such column"。建表与迁移都必须在这里
    补上，且不能碰系统库。
    """
    from sqlalchemy import create_engine as _create_engine

    from . import models as _models

    path.parent.mkdir(parents=True, exist_ok=True)
    target = _create_engine(
        sqlite_url(path), connect_args={"check_same_thread": False, "timeout": 30}
    )
    try:
        _models.Base.metadata.create_all(bind=target)
        _apply_lightweight_migrations(target)
        _apply_round3_migrations(target)
    finally:
        target.dispose()


def _apply_round3_migrations(target_engine=None) -> None:
    """Round-3 lightweight migrations: add columns introduced for confirm-driven
    history writeback."""
    target_engine = target_engine or engine
    if not _is_sqlite(target_engine):
        return
    with target_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with connection.begin():
            options = _sqlite_columns(connection, "quote_options")
            if options and "recorded_final_price" not in options:
                connection.exec_driver_sql(
                    "ALTER TABLE quote_options ADD COLUMN recorded_final_price FLOAT"
                )
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()


def _sqlite_columns(connection, table: str) -> dict:
    """Map column name -> PRAGMA table_info row (cid, name, type, notnull, dflt, pk)."""
    return {row[1]: row for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def _sqlite_rebuild_table(connection, table_name: str, model_table, target_engine=None) -> None:
    """Rebuild a SQLite table from the current model schema, preserving data.

    SQLite cannot relax a NOT NULL constraint in place, so the table is
    recreated: build ``<name>_new`` from the model DDL, copy the columns that
    exist in both schemas, swap names and recreate the indexes.
    """
    old_columns = _sqlite_columns(connection, table_name)
    common = [column.name for column in model_table.columns if column.name in old_columns]
    column_list = ", ".join(common)
    ddl = str(CreateTable(model_table).compile(target_engine or engine)).strip().rstrip(";")
    ddl = ddl.replace(f"CREATE TABLE {table_name}", f"CREATE TABLE {table_name}_new", 1)
    connection.exec_driver_sql(ddl)
    connection.exec_driver_sql(
        f"INSERT INTO {table_name}_new ({column_list}) SELECT {column_list} FROM {table_name}"
    )
    connection.exec_driver_sql(f"DROP TABLE {table_name}")
    connection.exec_driver_sql(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
    for index in model_table.indexes:
        index.create(bind=connection, checkfirst=True)


def _apply_lightweight_migrations(target_engine=None) -> None:
    """Bring databases created by older versions up to the current schema."""
    from . import models

    target_engine = target_engine or engine
    if _is_sqlite(target_engine):
        with target_engine.connect() as connection:
            # FK enforcement is off by default in SQLite; keep it off (the app
            # relies on that default) and run the rebuilds with it off.  The
            # PRAGMA must run outside a transaction, hence the commit first.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                jobs = _sqlite_columns(connection, "quote_jobs")
                if jobs:
                    if jobs["customer_id"][3]:
                        # NOT NULL customer_id -> rebuild (also adds display_name)
                        _sqlite_rebuild_table(
                            connection, "quote_jobs", models.QuoteJob.__table__, target_engine
                        )
                        # 重建后表结构已是模型最新版，必须重新读取列信息，
                        # 否则下面的 ALTER 会对已存在的列重复添加。
                        jobs = _sqlite_columns(connection, "quote_jobs")
                    elif "display_name" not in jobs:
                        connection.exec_driver_sql(
                            "ALTER TABLE quote_jobs ADD COLUMN display_name VARCHAR(300)"
                        )
                    if "tax_rate" not in jobs:
                        connection.exec_driver_sql(
                            "ALTER TABLE quote_jobs ADD COLUMN tax_rate FLOAT DEFAULT 0.1"
                        )
                    for name, ddl_type in JOB_BINDING_COLUMNS.items():
                        if name not in jobs:
                            connection.exec_driver_sql(
                                f"ALTER TABLE quote_jobs ADD COLUMN {name} {ddl_type}"
                            )
                options = _sqlite_columns(connection, "quote_options")
                if options:
                    if options["history_quote_id"][3]:
                        # NOT NULL history_quote_id -> rebuild (also adds manual_* columns)
                        _sqlite_rebuild_table(
                            connection, "quote_options", models.QuoteOption.__table__, target_engine
                        )
                        options = _sqlite_columns(connection, "quote_options")
                    else:
                        for name, ddl_type in MANUAL_OPTION_COLUMNS.items():
                            if name not in options:
                                connection.exec_driver_sql(
                                    f"ALTER TABLE quote_options ADD COLUMN {name} {ddl_type}"
                                )
                    for name, ddl_type in OPTION_RECORD_COLUMNS.items():
                        if name not in options:
                            connection.exec_driver_sql(
                                f"ALTER TABLE quote_options ADD COLUMN {name} {ddl_type}"
                            )
                history = _sqlite_columns(connection, "history_quotes")
                if history:
                    for name, ddl_type in HISTORY_ROW_COLUMNS.items():
                        if name not in history:
                            connection.exec_driver_sql(
                                f"ALTER TABLE history_quotes ADD COLUMN {name} {ddl_type}"
                            )
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        return
    with target_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE quote_jobs ADD COLUMN IF NOT EXISTS display_name VARCHAR(300)")
        connection.exec_driver_sql("ALTER TABLE quote_jobs ADD COLUMN IF NOT EXISTS tax_rate FLOAT DEFAULT 0.1")
        connection.exec_driver_sql("ALTER TABLE quote_jobs ALTER COLUMN customer_id DROP NOT NULL")
        connection.exec_driver_sql("ALTER TABLE quote_options ALTER COLUMN history_quote_id DROP NOT NULL")
        for name, ddl_type in MANUAL_OPTION_COLUMNS.items():
            connection.exec_driver_sql(
                f"ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS {name} {ddl_type}"
            )
        for name, ddl_type in JOB_BINDING_COLUMNS.items():
            connection.exec_driver_sql(
                f"ALTER TABLE quote_jobs ADD COLUMN IF NOT EXISTS {name} {ddl_type}"
            )
        for name, ddl_type in OPTION_RECORD_COLUMNS.items():
            connection.exec_driver_sql(
                f"ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS {name} {ddl_type}"
            )
        for name, ddl_type in HISTORY_ROW_COLUMNS.items():
            connection.exec_driver_sql(
                f"ALTER TABLE history_quotes ADD COLUMN IF NOT EXISTS {name} {ddl_type}"
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
