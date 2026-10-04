"""Storyloom Studio — 片段域服务层（附录 02 §2 F1–F10 的服务端实现）。

SOURCE-03..08 逐子卡填充（F1 原子保存候选 → F4/F5 改名/摘要 → F6 确认 →
F7 退役 → F8 边界 → F9 拆分 → F10 合并）。

实现约束（冻结）：
- 重叠校验 = 写事务内全量比对（范围集合 CAS 的唯一写入点：
  F1/F8/F9/F10 的 apply 改 cas_revision，+1/写；其余命令不动 cas）；
- 候选与确认共同参与重叠校验（R04），边界相接合法，退役者不参与；
- 任何写命令要求 fragment 绑定版本 == 项目 active，否则
  precondition_failed(reason=source_revision_inactive)；
- 父+子同 flush 必须显式 flush 父行（UOW 不保证跨表插入序，01-a 裁定）;
- 幂等/属主 404 不泄漏/错误协议同 projects/service.py 头注。
"""

__all__: list[str] = []
