"""Versioned production policy. Versions are pinned when an author creates a work."""
WORKFLOWS = {
    'author-brainstorm-v1': {
        'name': '脑洞 · 作者自动制作',
        'labels': ['脑洞'],
        'modules': [
            {'id':'source','name':'原文理解与规则提取','executor':'director'},
            {'id':'design','name':'角色与场景设计','executor':'art_design'},
            {'id':'author_assets','name':'作者确认形象','gate':'author'},
            {'id':'trial','name':'参考图组合与版本检查','executor':'image'},
            {'id':'board','name':'剧本分镜与独立文本预审','executor':'director'},
            {'id':'frames','name':'绑定角色场景制作关键帧','executor':'image'},
            {'id':'video','name':'真实视频制作与供应商查询','executor':'video'},
            {'id':'author_film','name':'作者审片并发布','gate':'author'},
        ],
        'limits': ['当前只改编一个 4–8 镜短场景','独立配音和混音尚未实现','视觉一致性尚无自动视觉模型验收','读者改写目前只保存愿望，未生成新分支'],
    }
}

WORKFLOWS['author-brainstorm-v2']={
    **WORKFLOWS['author-brainstorm-v1'],
    'name':'脑洞 · 身份与服装分离制作',
    'asset_schema':'asset-sheets-v3',
    'modules':[
        {'id':'source','name':'原文理解与规则提取','executor':'director'},
        {'id':'identity','name':'专业画风与唯一人物身份','executor':'art_design','checkpoint':'identity_plan'},
        {'id':'wardrobe_sets','name':'独立服装与物理场景','executor':'art_design','checkpoint':'wardrobe_scene_plan','depends_on':['identity']},
        {'id':'author_assets','name':'确认身份、服装、场景','gate':'author'},
        {'id':'fittings','name':'人物身份与服装参考合成定装','executor':'image','depends_on':['author_assets']},
        {'id':'trial','name':'定装与场景组合试拍','executor':'image','depends_on':['fittings']},
        {'id':'board','name':'引用固定身份与定装的分镜','executor':'director','depends_on':['trial']},
        *WORKFLOWS['author-brainstorm-v1']['modules'][5:],
    ],
}

WORKFLOWS['author-brainstorm-v3']={
    **WORKFLOWS['author-brainstorm-v2'],
    'name':'脑洞 · 专业 Skill 节点制作','node_skill_version':'1.0.0',
    'modules':[
        {'id':'source','name':'剧情改编与原文依据','executor':'director','skill_node':'story_treatment'},
        {'id':'style','name':'基础美术渲染参数','executor':'art_design','skill_node':'style_spec','checkpoint':'style_plan'},
        {'id':'identity','name':'唯一人物身份','executor':'art_design','skill_node':'identity_spec','checkpoint':'character_plan','depends_on':['source','style']},
        {'id':'costumes','name':'独立服装规格','executor':'art_design','skill_node':'costume_spec','checkpoint':'costume_plan','depends_on':['identity']},
        {'id':'scenes','name':'物理场景规格','executor':'art_design','skill_node':'scene_spec','checkpoint':'scene_plan','depends_on':['source','style']},
        {'id':'asset_prompts','name':'资产生图提示词编写','executor':'art_design','skill_node':'asset_prompts','checkpoint':'image_prompt_batches','depends_on':['identity','costumes','scenes']},
        {'id':'author_assets','name':'确认基础素材','gate':'author','skill_node':'asset_review','depends_on':['asset_prompts']},
        {'id':'fittings','name':'身份与服装合成','executor':'image','skill_node':'fitting','depends_on':['author_assets']},
        {'id':'trial','name':'定装与场景试拍','executor':'image','skill_node':'scene_trial','depends_on':['fittings']},
        {'id':'board','name':'专业镜头规划','executor':'director','skill_node':'storyboard','depends_on':['trial']},
        {'id':'shot_prompts','name':'分镜图像与视频提示词','executor':'director','skill_node':'shot_prompts','depends_on':['board']},
        {'id':'board_review','name':'分镜文本预审','executor':'director','skill_node':'storyboard_review','depends_on':['shot_prompts']},
        {'id':'frames','name':'关键帧生成','executor':'image','skill_node':'frame_render','depends_on':['board_review']},
        {'id':'video','name':'视频生成','executor':'video','skill_node':'video_render','depends_on':['frames']},
        {'id':'author_film','name':'成片人工验收','gate':'author','skill_node':'film_review','depends_on':['video']},
    ],
}

WORKFLOWS['author-brainstorm-v4']={
    **WORKFLOWS['author-brainstorm-v3'],
    'name':'脑洞 · 专业参考图制作','node_skill_version':'1.3.1','video_input_mode':'reference_images',
    'modules':[{**module,**({'name':'镜头参考图生成'} if module['id']=='frames' else
                          {'name':'图片参考视频生成'} if module['id']=='video' else {})}
               for module in WORKFLOWS['author-brainstorm-v3']['modules']],
}

WORKFLOWS['author-brainstorm-v5']={
    **WORKFLOWS['author-brainstorm-v4'],
    'name':'脑洞 · 分节点制作','node_skill_version':'1.6.0','revision_mode':'full_prompt_regeneration',
    'modules':[{**module,**({'depends_on':['fittings']} if module['id']=='board' else {})} for module in WORKFLOWS['author-brainstorm-v4']['modules'] if module['id']!='trial'],
    'production_coordinators':[
        {'id':'compositing','name':'图像合成','executor':'author_composite','modules':['fittings'],'output':'已确认定装与场景参考图'},
        {'id':'storyboarding','name':'分镜生成','executor':'author_storyboard','depends_on':['compositing'],'modules':['board','shot_prompts','board_review'],'output':'通过文本预审的分镜与镜头提示词'},
        {'id':'rendering','name':'漫剧生成','executor':'author_render','depends_on':['storyboarding'],'modules':['frames','video'],'output':'已保存的视频片段'},
    ],
}

WORKFLOWS['author-brainstorm-v6']={
    **WORKFLOWS['author-brainstorm-v5'],
    'name':'脑洞 · 基础参考图直接分镜','node_skill_version':'1.6.0',
    'modules':[{**module,**({'depends_on':['author_assets']} if module['id']=='board' else {})} for module in WORKFLOWS['author-brainstorm-v5']['modules'] if module['id'] not in ('fittings','trial')],
    'production_coordinators':[
        {'id':'storyboarding','name':'分镜生成','executor':'author_storyboard','modules':['board','shot_prompts','board_review'],'output':'直接组合人物身份、服装与场景参考图的分镜及提示词'},
        {'id':'rendering','name':'漫剧生成','executor':'author_render','depends_on':['storyboarding'],'modules':['frames','video'],'output':'已保存的镜头参考图与视频片段'},
    ],
}
