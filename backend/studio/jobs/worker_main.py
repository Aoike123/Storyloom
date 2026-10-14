"""Storyloom Studio — 新 worker 进程入口（JOB-03）。

启动方式::

    .venv/bin/python -m backend.studio.jobs.worker_main

单进程轮询作业表、按 job kind 分派处理器（附录 09 §5）。**不复用**旧
``backend.worker``（旧 Record/车道/读者分支不属于新流程）。

**import 本模块不得有副作用**：不建表、不连库、不启线程——建表由
``backend.studio.app_factory`` 的 lifespan 负责；本模块只在 ``main()`` 被调用时才
构造 runner 并进入轮询。
"""
from __future__ import annotations

import argparse
import os
import socket

from .runner import JobRunner, default_handlers

__all__ = ["main"]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.studio.jobs.worker_main",
        description="Storyloom Studio 新 worker（作业租约执行）。",
    )
    parser.add_argument("--worker-id", default=None,
                        help="worker 标识（缺省 <hostname>-<pid>）")
    parser.add_argument("--poll-seconds", type=float, default=0.5,
                        help="空队列轮询间隔秒（缺省 0.5）")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="最多轮询轮数后退出（便于确定性测试；缺省不限）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    worker_id = args.worker_id or _default_worker_id()
    runner = JobRunner(worker_id=worker_id, handlers=default_handlers())
    runner.run_forever(
        poll_seconds=args.poll_seconds, max_iterations=args.max_iterations
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
