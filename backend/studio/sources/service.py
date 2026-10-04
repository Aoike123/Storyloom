"""Storyloom Studio — 来源域服务层（附录 01 §2 S1–S6 的服务端实现）。

SOURCE-01-d/e 逐子卡填充（S1 导入不可变原文、S3 读取来源版本）；
S2/S4/S5/S6 属 SOURCE-03+ 后续卡。

实现约束（冻结）：
- raw 原样保存：不 trim、不 Unicode 归一化（附录 01 §1）；
- canonical = CRLF/CR→LF（其余不变）；哈希 = sha256(UTF-8)；
  char_length = canonical 的 UTF-16 code unit 数——三者必须用
  backend/studio/sources/ranges.py 的 canonicalize/utf16_len（附录 01 §4：
  与附录 00 §7 同源，共享 SOURCE-02 helper）；
- offset_policy 固定 'lf-utf16-v1'；无 UPDATE/DELETE 命令（R03/R11）；
- 幂等/属主/错误协议同 projects/service.py 头注。
"""

__all__: list[str] = []
