"""供应商适配器包（ROOT-04 附录 09 §5）：只读抽取的纯协议，无旧后端业务依赖。

子模块：`base`（错误分类/HTTPS 校验/配置门槛/注入式 transport）、
`llm`（DeepSeek OpenAI 兼容文本）、`image`（硅基流动生图）、
`video`（MiniMax H3 / 火山方舟 Tasks）。

本包不 import 旧 backend 业务模块，不读写旧 Task/Record，不写数据库，
所有出网必须经调用方注入的 transport（缺省才是 httpx）。
"""
