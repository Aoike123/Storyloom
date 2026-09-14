"""Compile plot-free style, identity, costume and set specifications."""
from typing import Annotated
from pydantic import Field, model_validator
from .visual_specs import (Spec, VisualStyle, CharacterSheet, CostumeSheet, SceneSheet,
                           description, style_prompt, validate_description_budget)

VERSION = 'asset-sheets-v3'
KINDS = {'character': 'character_sheet', 'costume': 'costume_sheet', 'scene': 'scene_sheet'}
MODELS = {'character': CharacterSheet, 'costume': CostumeSheet, 'scene': SceneSheet}
MAX_CHARACTERS = 6
MAX_MATERIALS = 12
PROMPT_BATCH_SIZE = 2


class IdentityPlan(Spec):
    visual_style: VisualStyle
    characters: list[CharacterSheet] = Field(min_length=1, max_length=MAX_CHARACTERS)

    @model_validator(mode='after')
    def unique_identities(self):
        if len({c.character_id for c in self.characters}) != len(self.characters) or len({c.name for c in self.characters}) != len(self.characters):
            raise ValueError('人物身份不能重复；换服装不能新增人物身份')
        return self


class WardrobeScenePlan(Spec):
    items: list[Annotated[CostumeSheet | SceneSheet, Field(discriminator='role')]] = Field(min_length=2, max_length=MAX_MATERIALS)


class StylePlan(Spec):
    visual_style: VisualStyle


class CharacterPlan(Spec):
    characters: list[CharacterSheet] = Field(min_length=1,max_length=MAX_CHARACTERS)


class CostumePlan(Spec):
    costumes: list[CostumeSheet] = Field(min_length=1,max_length=MAX_MATERIALS-1)


class ScenePlan(Spec):
    scenes: list[SceneSheet] = Field(min_length=1,max_length=MAX_MATERIALS-1)


class AssetPrompt(Spec):
    asset_index: int = Field(ge=0)
    prompt: str = Field(min_length=30,max_length=3000)


class AssetPromptBatch(Spec):
    items: list[AssetPrompt] = Field(min_length=1,max_length=PROMPT_BATCH_SIZE)


class AssetSheetPlan(Spec):
    visual_style: VisualStyle
    items: list[Annotated[CharacterSheet | CostumeSheet | SceneSheet, Field(discriminator='role')]] = Field(min_length=3, max_length=MAX_CHARACTERS + MAX_MATERIALS)

    @model_validator(mode='after')
    def asset_bindings(self):
        characters = [item.character_id for item in self.items if item.role == 'character']
        costumes = [item for item in self.items if item.role == 'costume']
        if not characters or not any(item.role == 'scene' for item in self.items):
            raise ValueError('至少需要一份人物身份和一个物理场景')
        if len(characters) != len(set(characters)):
            raise ValueError('同一人物只能有一个身份编号，不能按服装拆分')
        if len({item.costume_id for item in costumes}) != len(costumes):
            raise ValueError('服装编号重复')
        if {item.character_ref for item in costumes} != set(characters):
            raise ValueError('每套服装必须绑定已有身份，每个人物至少绑定一套服装')
        names = [item.name for item in self.items if item.role == 'character']
        if len(set(names)) != len(names):
            raise ValueError('同一人物不能重复建立身份')
        for item in self.items:
            validate_description_budget(item)
        return self


def frame(kind):
    if kind == 'character_sheet':
        return ('人物身份三视图设定板。正面、左侧面、背面三个全身视图从左到右并排；'
                '同一脸型、发型、身材和固定特征，等比例、等身高、同一地面基线，从头到脚完整可见。'
                '自然中立站姿，手臂略离躯干，手脚清晰。固定浅灰圆领短袖上衣、直筒长裤与平底鞋；'
                '完整着装，不使用紧身透视或裸露造型，基础服装只作身份比例参考。'
                '浅灰纯色背景、均匀中性灯光、正交视角，每个视图清晰对焦。')
    if kind == 'costume_sheet':
        return ('独立服装设定板。指定衣物的正面、左侧面、背面从左到右等比例展示；'
                '衣服保持立体裁剪形状，展示领口、袖型、门襟、下摆、缝线和配饰。'
                '仅衣物，空心无模特，不出现头脸、皮肤、人体、肢体或穿着者；鞋履内部不出现脚或腿。'
                '浅灰纯色背景，均匀中性照明，清晰呈现版型、面料和颜色。')
    if kind == 'scene_sheet':
        return ('空场景空间设定板。清晰无人全景，右下角附同一空间的俯视布局图；'
                '各视图的门窗、出入口、家具、静物位置、空间比例和材质一致。'
                '完整展示空间连接和可通行区域，不出现人物或人形剪影。')
    raise ValueError('未知设定图类型')


def compose_prompt(style, item):
    validate_description_budget(item)
    return '\n\n'.join([frame(KINDS[item.role]), style_prompt(style), description(item),
                           '画面限制：无文字、无标签、无对话框、无界面元素、无水印；不画色卡、色块样本、材质样本或说明图例；保持固定展示版式。'])
