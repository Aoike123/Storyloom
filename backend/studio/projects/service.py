"""Storyloom Studio — 项目域服务层（附录 01 §2 命令的服务端实现）。

SOURCE-01-a..e 逐子卡填充（每卡只实现其唯一命令）：
P1 新建 / P3 读取 / P2 列表 /（来源侧 S1/S3 在 sources/service.py）。

实现约束（冻结）：
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401；跨属主/不存在一律 404 not_found（不泄漏存在性，
  SOURCE-01 验收裁定：读写统一 404）；
- id 全部服务端 ULID（core.db.make_ulid）；
- 错误一律 raise StudioAPIError（冻结错误协议）。
"""

__all__: list[str] = []
