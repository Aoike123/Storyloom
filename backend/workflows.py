"""Versioned production policy. Versions are pinned when an author creates a work.

One line is live. The retired pre-v7 lines defined 定装合成、试拍 and 镜头参考图, three steps the
current line no longer contains; they are gone together with the code that ran them.
"""

WORKFLOWS = {
    'author-brainstorm-v7': {
        'name': '脑洞 · 微小说切割后分块分镜',
        'labels': ['脑洞'],
        'asset_schema': 'asset-sheets-v4',
        'node_skill_version': '1.8.0',
        'video_input_mode': 'reference_images',
        'revision_mode': 'full_prompt_regeneration',
        'segmentation_node': 'story_segments',
        'limits': ['先按原文把微小说切成连续片段，再逐片段规划镜头；全部片段合计不超过 24 镜',
                   '独立配音和混音尚未实现','视觉一致性尚无自动视觉模型验收',
                   '读者分支只复用当前场景、人物与着装，真实视觉命中仍需样片验收'],
        'modules': [
            {'id':'source','name':'剧情改编与原文依据','executor':'director','skill_node':'story_treatment'},
            {'id':'style','name':'基础美术渲染参数','executor':'art_design','skill_node':'style_spec','checkpoint':'style_plan'},
            {'id':'identity','name':'唯一人物身份','executor':'art_design','skill_node':'identity_spec','checkpoint':'character_plan','depends_on':['source','style']},
            {'id':'costumes','name':'独立服装规格','executor':'art_design','skill_node':'costume_spec','checkpoint':'costume_plan','depends_on':['identity']},
            {'id':'scenes','name':'物理场景规格','executor':'art_design','skill_node':'scene_spec','checkpoint':'scene_plan','depends_on':['source','style']},
            {'id':'character_prompts','name':'专业角色身份图提示词编写','executor':'art_design','skill_node':'character_prompts','depends_on':['identity']},
            {'id':'asset_prompts','name':'服装与场景生图提示词编写','executor':'art_design','skill_node':'asset_prompts','checkpoint':'image_prompt_batches','depends_on':['character_prompts','costumes','scenes']},
            {'id':'author_assets','name':'确认基础素材','gate':'author','skill_node':'asset_review','depends_on':['asset_prompts']},
            {'id':'segments','name':'微小说片段切割','executor':'director','skill_node':'story_segments','depends_on':['author_assets']},
            {'id':'board','name':'按片段分块规划镜头','executor':'director','skill_node':'storyboard','depends_on':['segments']},
            {'id':'shot_prompts','name':'分镜图像与视频提示词','executor':'director','skill_node':'shot_prompts','depends_on':['board']},
            {'id':'board_review','name':'分镜文本预审','executor':'director','skill_node':'storyboard_review','depends_on':['shot_prompts']},
            {'id':'video','name':'图片参考视频生成','executor':'video','skill_node':'video_render','depends_on':['board_review']},
            {'id':'author_film','name':'成片人工验收','gate':'author','skill_node':'film_review','depends_on':['video']},
        ],
        'production_coordinators': [
            {'id':'storyboarding','name':'分镜生成','executor':'author_storyboard','modules':['segments','board','shot_prompts','board_review'],'output':'先切割微小说，再逐片段分块生成并合并的分镜、镜头提示词与文本预审'},
            {'id':'rendering','name':'漫剧生成','executor':'author_render','depends_on':['storyboarding'],'modules':['video'],'output':'以已审核参考图直接生成的视频片段'},
        ],
    }
}
