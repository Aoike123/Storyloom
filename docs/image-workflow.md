# 生图到视频的小样流程

在模型连接页填写并保存三类 API 配置。生图当前支持硅基流动协议，预设模型为 `Tongyi-MAI/Z-Image-Turbo`，中国区接口为 `https://api.siliconflow.cn/v1/images/generations`。国际区账号使用对应的 `.com` 接口；不同平台的 Key 不可混用。其他生图协议尚未适配。

1. 设置累计提交额度，并勾选允许付费调用；保存本身不调用模型。
2. 在“关键帧 → 视频”填写画面描述，确认收费，点击“生成关键帧”。后台执行器必须运行。
3. 任务完成后，从关键帧下拉框选择图片，查看后点击“确认图片可用”。图片同时进入人物与场景素材库。
4. 填写运动描述，点击“用该关键帧生成视频”。该步骤使用已审核本地图片作为 MiniMax 首帧，无需图床。
5. 视频完成后在原版制作页预览、审核和标注。上传或生成小样不等于发布完整故事。

当前生成图为 1024×1024。图片和视频各占一次提交额度；次数不是金额。生图中断或结果异常不会自动再次收费；已有任务完成或停止后才允许修改供应商配置。图片保存为本地 PNG，避免供应商临时 URL 过期影响后续制作。这里尚未实现人物参考图编辑或整篇故事自动分镜。

验证：34 项离线测试通过，前端类型检查通过，浏览器检查已确认生图配置及关键帧流程可见。真实收费调用尚未验证。

协议来源：[硅基流动生图接口](https://api-docs.siliconflow.cn/docs/api/images-generations-post)、[Z-Image-Turbo 官方托管示例](https://www.siliconflow.com/zh/blog/z-image-turbo-now-on-siliconflow-photorealistic-bilingual-text-rendering)、[MiniMax H3 V2](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)。
