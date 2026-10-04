"""Storyloom Studio — 新数据库连接与 Base。

与旧后端 db 模块完全隔离：独立数据根 data/studio、独立 env 变量名
（STUDIO_DATA_DIR / STUDIO_DATABASE_URL，避免与旧 app 的 DATA_DIR/DATABASE_URL
互相干扰）。不 import 任何旧后端模块。
"""
import os
import secrets
import time
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


# Crockford base32 字母表（ULID 标准编码）
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def make_ulid() -> str:
    """服务端 ULID：49 位毫秒时间戳 + 80 位随机，Crockford base32（26 字符，小写）。

    附录 02 冻结：所有领域对象 id 由服务端生成（客户端不生成 id）；
    26 字符兼容全部 String(32) 主键槽。
    """
    ts = int(time.time() * 1000)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    n = (ts << 80) | rand
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def init_db() -> None:
    """按 Base.metadata 中已注册的模型建表。

    调用方先 import 相关模型模块（import 即注册），再调用本函数。
    不 drop、不迁移、不回填。
    """
    Base.metadata.create_all(engine)
