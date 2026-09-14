---
name: frame-render
description: 镜头参考图生成请求编排，只编排已批准参数。
---

# 镜头参考图生成请求编排

由程序加载此模板，以已批准的参考图和专业 Prompt 填充；不再做剧情或资产设计。

## Template
根据已审核参考图生成单个镜头参考画面，用于视频的构图、定装和环境外观参考。
统一视觉：$style
参考图顺序与身份约束：$references
本镜连续性：$continuity
参考画面：$reference_prompt
保持参考身份、服装、空间与画风，画面不添加字幕或多个剧情格。
