"""Storyloom Studio — 新数据库连接与 Base。

与旧后端 db 模块完全隔离：独立数据根 data/studio、独立 env 变量名
（STUDIO_DATA_DIR / STUDIO_DATABASE_URL，避免与旧 app 的 DATA_DIR/DATABASE_URL
互相干扰）。不 import 任何旧后端模块。
"""
import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# 仓库根（本文件位于 <root>/backend/core/db.py）
ROOT = Path(__file__).resolve().parents[2]

# ROOT-01 冻结的数据根；新系统专用 env 名。
DATA_DIR = Path(os.getenv("STUDIO_DATA_DIR", str(ROOT / "data" / "studio"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "media").mkdir(exist_ok=True)

DATABASE_URL = os.getenv("STUDIO_DATABASE_URL", f"sqlite:///{DATA_DIR / 'story.db'}")


def build_engine(url: str) -> Engine:
    """sqlite：check_same_thread=False + 30s 超时 + 每连接 WAL + 外键开启；pool_pre_ping。"""
    engine = create_engine(
        url,
        connect_args=(
            {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        ),
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


engine = build_engine(DATABASE_URL)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """按 Base.metadata 中已注册的模型建表。

    调用方先 import 相关模型模块（import 即注册），再调用本函数。
    不 drop、不迁移、不回填。
    """
    Base.metadata.create_all(engine)
