"""Compile plot-free style, identity, costume and set specifications."""
from typing import Annotated
from pydantic import Field, model_validator
from .visual_specs import (Spec, VisualStyle, CharacterSheet, CostumeSheet, SceneSheet, CreatureAppearance,
                           description, style_prompt, validate_description_budget, validate_style_intent)

VERSION = 'asset-sheets-v4'
KINDS = {'character': 'character_sheet', 'costume': 'costume_sheet', 'scene': 'scene_sheet'}
MODELS = {'character': CharacterSheet, 'costume': CostumeSheet, 'scene': SceneSheet}
MAX_CHARACTERS = 6
MAX_MATERIALS = 12
PROMPT_BATCH_SIZE = 2

# Which parts of a body are human. Whether the identity sheet must cover the character is decided
# by anatomy first, never by the plan name: 兽首人身 is a beast head on a human torso, so asking it
# to "完整展示自然体表" asks for a naked human body, which the provider refuses as prohibited
# content. Only a body with no human part may show its natural surface (fur, scales, feathers,
# shell), and only when the story assigns it no clothing either.
HUMAN_BODY_PARTS=(('躯干','torso'),('臂手','arms_hands'),('腿足','legs_feet'))


def human_body_regions(appearance):
    """Labels of the human body regions, in the order the prompt names them. Empty for pure beasts."""
    return [label for label,field in HUMAN_BODY_PARTS
            if (part:=getattr(appearance,field,None)) is not None and part.nature=='人类']


class IdentityPlan(Spec):
    visual_style: VisualStyle
    characters: list[CharacterSheet] = Field(min_length=1, max_length=MAX_CHARACTERS)

    @model_validator(mode='after')
    def unique_identities(self):
        if len({c.character_id for c in self.characters}) != len(self.characters) or len({c.name for c in self.characters}) != len(self.characters):
            raise ValueError('人物身份不能重复；换服装不能新增人物身份')
        return self


class WardrobeScenePlan(Spec):
    items: list[Annotated[CostumeSheet | SceneSheet, Field(discriminator='role')]] = Field(min_length=1, max_length=MAX_MATERIALS)


class StylePlan(Spec):
    visual_style: VisualStyle


class CharacterPlan(Spec):
    characters: list[CharacterSheet] = Field(min_length=1,max_length=MAX_CHARACTERS)


class CostumePlan(Spec):
    costumes: list[CostumeSheet] = Field(default_factory=list,max_length=MAX_MATERIALS-1)


class ScenePlan(Spec):
    scenes: list[SceneSheet] = Field(min_length=1,max_length=MAX_MATERIALS-1)


class AssetPrompt(Spec):
    asset_index: int = Field(ge=0)
    prompt: str = Field(min_length=30,max_length=3000)


class AssetPromptBatch(Spec):
    items: list[AssetPrompt] = Field(min_length=1,max_length=PROMPT_BATCH_SIZE)


class AssetSheetPlan(Spec):
    visual_style: VisualStyle
    items: list[Annotated[CharacterSheet | CostumeSheet | SceneSheet, Field(discriminator='role')]] = Field(min_length=2, max_length=MAX_CHARACTERS + MAX_MATERIALS)

    @model_validator(mode='after')
    def asset_bindings(self):
        character_items = [item for item in self.items if item.role == 'character']
        characters = [item.character_id for item in character_items]
        costumes = [item for item in self.items if item.role == 'costume']
        if not characters or not any(item.role == 'scene' for item in self.items):
            raise ValueError('至少需要一份人物身份和一个物理场景')
        if len(characters) != len(set(characters)):
            raise ValueError('同一人物只能有一个身份编号，不能按服装拆分')
        if len({item.costume_id for item in costumes}) != len(costumes):
            raise ValueError('服装编号重复')
        costume_characters={item.character_ref for item in costumes}
        if not costume_characters <= set(characters):
            raise ValueError('每套服装必须绑定已有身份')
        # Character -> costume -> set is one sequence for every role, so a character can never
        # drop out of the costume stage. Natural bodies answer it with the empty-clothing mode.
        # Several outfits for one character stay legal: those are wardrobe alternatives, not
        # separate people, and the storyboard picks one per shot.
        owners={item.character_id:item for item in character_items}
        missing=[item.character_id for item in character_items if item.character_id not in costume_characters]
        if missing:
            raise ValueError('每个角色都必须至少有一条服装记录；不着衣物的天然体表角色请使用空衣服模式（mode=bare），'
                             '不要省略记录。缺少服装记录：'+'、'.join(missing))
        for costume in costumes:
            owner=owners[costume.character_ref]
            if costume.mode=='bare':
                # The empty-clothing mode answers "this species wears no story clothing". A body
                # with human regions would be rendered naked, which the provider refuses and which
                # no costume record can fix, so those characters must bring real clothing.
                regions=human_body_regions(owner.appearance) if isinstance(owner.appearance,CreatureAppearance) else []
                if regions:
                    raise ValueError(f'「{owner.name}」不能使用空衣服模式：它是兽首人身一类带人类{"、".join(regions)}的角色，'
                                     '空衣服模式会把它的人类身体画成裸露并被供应商判定违规；请为该角色给出实际服装')
                if not isinstance(owner.appearance,CreatureAppearance) or owner.costume_mode!='none':
                    raise ValueError(f'「{owner.name}」不是天然体表的不着衣物角色，不能使用空衣服模式；'
                                     '人类或有服装需求的角色必须给出实际服装，避免身份图上的基础服装被当成最终衣着')
            else:
                if owner.costume_mode=='none':
                    raise ValueError(f'「{owner.name}」标记为 costume_mode=none，不能绑定实际服装；请改用空衣服模式')
        bare_characters={costume.character_ref for costume in costumes if costume.mode=='bare'}
        mixed=sorted(bare_characters & {costume.character_ref for costume in costumes if costume.mode=='garment'})
        if mixed:
            raise ValueError('同一角色的空衣服模式与实际服装不能同时存在：'+'、'.join(mixed)+
                             '。该角色到底着不着衣物只能有一个答案。')
        names = [item.name for item in self.items if item.role == 'character']
        if len(set(names)) != len(names):
            raise ValueError('同一人物不能重复建立身份')
        for item in self.items:
            validate_description_budget(item)
        return self


def covering_text(appearance,costume_mode):
    """What the identity sheet puts on the body: anatomy decides first, clothing decides the rest."""
    regions=human_body_regions(appearance)
    if regions:
        return ('不添加剧情服装、盔甲、法器或饰品；人类'+'、'.join(regions)+
                '必须以无标识、低遮挡的中性基础短装覆盖，保留必要遮盖，不做裸露、紧身透视或性化呈现；'
                '非人类分区的毛皮、鳞片、羽毛或甲壳完整露出，不被衣物遮住物种结构。')
    if appearance.body_plan in ('拟人双足','兽首人身') and costume_mode!='none':
        # A clothed humanoid still gets the neutral base outfit, so the identity sheet never looks
        # undressed while the story costume stays in its own sheet.
        return '不添加剧情服装、盔甲、法器或饰品；仅使用无标识、低遮挡的中性基础短装，不遮住体表和物种结构。'
    return '不添加人类服装、盔甲、法器或饰品，完整展示该物种自然体表与身体结构。'


def frame(kind,item=None):
    if kind=='costume_sheet' and getattr(item,'mode','garment')=='bare':
        raise ValueError('空衣服模式不生成服装设定图：该角色的自然体表已由人物身份图承载。')
    if kind == 'character_sheet':
        if item is not None and isinstance(item.appearance,CreatureAppearance):
            clothing=covering_text(item.appearance,item.costume_mode)
            return ('神话生物或非人角色身份三视图设定板。正面、左侧面、背面三个全身或全体视图从左到右并排；'
                    '同一物种、同一头身分区、体表、肢体、尾翼角及固定特征，等比例、等尺寸、同一地面基线，主体完整可见。'
                    '头部和颈部以下身体必须分别服从结构化分区，不得把头部物种特征扩散到人身或把妖身替换成人身。'
                    '中立静止姿态，不做剧情动作，不得改成人类，不得用人类脸型、发型或肤色覆盖物种特征。'+clothing+
                    '浅灰纯色背景、均匀中性灯光、正交视角，每个视图清晰对焦。')
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
    return '\n\n'.join([frame(KINDS[item.role],item), style_prompt(style), description(item),
                           '画面限制：无文字、无标签、无对话框、无界面元素、无水印；不画色卡、色块样本、材质样本或说明图例；保持固定展示版式。'])
