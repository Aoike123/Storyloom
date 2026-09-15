"""Image outputs are references with different roles, not video boundary frames."""


def completed_message(payload):
    kind=payload.get('asset_kind')
    if payload.get('edit_instruction'):return '修改后的参考图已保存，请检查画面是否落实本次修改，再确认用于后续制作。'
    if kind=='character_sheet':return '人物身份参考图已保存，确认后用于定装合成与人物一致性参考。'
    if kind=='costume_sheet':return '服装参考图已保存，确认后与对应人物身份图合成定装。'
    if kind=='scene_sheet':return '场景参考图已保存，确认后用于空间、布局与光照参考。'
    if kind=='dressed_character':return '定装参考图已保存，确认后用于试拍、分镜与视频参考。'
    if payload.get('preproduction_id'):return '组合试拍参考图已保存，请检查人物、服装和场景的一致性。'
    if payload.get('director_id'):return '历史镜头参考图已保存，仅作记录保留。'
    return '参考图片已保存，请审核后用于后续制作。'
