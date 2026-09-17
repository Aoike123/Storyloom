"""Image outputs are references with different roles, not video boundary frames."""


def completed_message(payload):
    kind=payload.get('asset_kind')
    if payload.get('edit_instruction'):return '修改后的参考图已保存，请检查画面是否落实本次修改，再确认用于后续制作。'
    if kind=='character_sheet':return '人物身份参考图已保存，确认后与该角色的独立服装一起用于分镜与视频参考。'
    if kind=='costume_sheet':return '服装参考图已保存，确认后与该角色的人物身份图一起用于分镜与视频参考。'
    if kind=='scene_sheet':return '场景参考图已保存，确认后用于空间、布局与光照参考。'
    return '参考图片已保存，请审核后用于后续制作。'
