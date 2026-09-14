"""Validated physical attributes and professional rendering parameters."""
import re
from typing import Annotated, Literal
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def static_phrase(value):
    value=value.strip()
    if not value:
        raise ValueError('静态物理属性不能为空')
    if re.search(r'^[我你他她它]|[。！？!?\r\n“”「」]|因为|所以|为了|于是|然后|随后|从而|仿佛|好像|似乎|大概|可能|建议|不详|未知|未提供|未明确|或|体现|象征|暗示|寓意|剧情|故事|观众|弹幕|直播|台词|分镜|蒙太奇|主观视角|失焦|信息差|击飞|挥手|拥抱|打斗|攻击|斥责|护住|逃跑|走进|站在|荒诞|温馨|惊悚|恐怖|性感|气质|氛围|主角|角色|风格|一样|看起来|略显|普通',value):
        raise ValueError('只允许明确的静态物理属性，不能包含剧情、情绪、因果或不确定表达')
    if re.search(r'\b(maybe|perhaps|story|narrative|viewer|audience|dialogue|storyboard|symbolize|attacking)\b',value,re.I):
        raise ValueError('只允许明确的静态物理属性')
    return value


Phrase=Annotated[str,Field(min_length=2,max_length=72),AfterValidator(static_phrase)]
ObjectCategory=Annotated[str,Field(min_length=1,max_length=72),AfterValidator(static_phrase)]
Color=Annotated[str,Field(pattern=r'^#[0-9A-Fa-f]{6}$')]
Material=Literal['棉布','亚麻','羊毛','针织','皮革','牛仔布','涤纶','丝绸','橡胶','木材','石材','混凝土','砖','灰泥','石膏板','瓷砖','钢材','玻璃','塑料','金属']
Finish=Literal['哑光','半哑光','高光','粗糙','拉丝','磨砂','织纹','做旧']


def canonical_light_direction(value):
    if not isinstance(value,str):return value
    value=value.strip()
    aliases={
        '北向南':'北侧向南','南向北':'南侧向北','东向西':'东侧向西','西向东':'西侧向东',
        '东北侧向西南':'东北向西南','东南侧向西北':'东南向西北',
        '西南侧向东北':'西南向东北','西北侧向东南':'西北向东南',
        '由东北向西南':'东北向西南','由东南向西北':'东南向西北',
        '由西南向东北':'西南向东北','由西北向东南':'西北向东南',
    }
    return aliases.get(value,value)


LightSource=Literal['漫射自然光','窗户日光','顶置灯板','吊灯','壁灯','落地灯','台灯','路灯','阴天天光','定向日光']
LightDirection=Annotated[Literal['顶部向下','北侧向南','南侧向北','东侧向西','西侧向东',
    '东北向西南','东南向西北','西南向东北','西北向东南','均匀环境光'],BeforeValidator(canonical_light_direction)]


class Spec(BaseModel):
    model_config=ConfigDict(extra='forbid')


class PaletteColor(Spec):
    role: Literal['主色','辅色','点缀色']
    hex: Color
    percent: int=Field(ge=1,le=95,description='画面色彩占比，色板各项合计100')


class VisualStyle(Spec):
    medium: Literal['二维数字蘸水笔勾线','赛璐璐动画原画','透明水彩绘制','不透明水粉绘制','油彩厚涂','石墨铅笔素描','木刻版画','像素绘制','风格化三维渲染','写实三维渲染']
    linework: Literal['外轮廓粗线、内部结构细线、笔压粗细变化','等粗细清洁描线','干笔断续线条','交叉排线塑形','铅笔轮廓与擦痕','像素网格硬边','无轮廓线、以色块和明暗区分形体']
    shading: Literal['平涂色块','两阶硬边赛璐璐阴影','三阶硬边赛璐璐阴影','网点明暗','平行排线明暗','湿画法透明叠色','厚涂笔触塑形','平滑渐变塑形','物理材质明暗']
    palette: list[PaletteColor]=Field(min_length=3,max_length=5)
    saturation: Literal['低饱和度','中等饱和度','高饱和度']
    contrast: Literal['柔和明度对比','中等明度对比','高明度对比、暗部保留层次']
    edge_treatment: Literal['锐利清晰边缘','轮廓硬边、内部软边','干笔毛边','像素阶梯边缘']
    surface_texture: Literal['平滑哑光表面','细颗粒绘图纸纹理','粗纹水彩纸纹理','可见画布纹理','木刻刀痕纹理','低密度胶片颗粒']

    @model_validator(mode='after')
    def palette_balance(self):
        if sum(c.percent for c in self.palette)!=100:raise ValueError('色板占比必须合计100')
        if sum(c.role=='主色' for c in self.palette)!=1:raise ValueError('色板必须且只能指定一个主色')
        if len({c.hex.lower() for c in self.palette})!=len(self.palette):raise ValueError('色板颜色不能重复')
        return self


class HumanAppearance(Spec):
    age_band: Literal['儿童','少年','青年成人','中年成人','老年成人']
    presentation: Literal['女性外观','男性外观','中性外观']
    height_cm: int=Field(ge=60,le=250)
    head_body_ratio: float=Field(ge=3,le=9,description='头身比，例如7.5')
    build: Literal['纤细体型','标准体型','宽肩体型','结实体型','丰满体型']
    face_shape: Literal['椭圆脸','圆脸','方脸','长脸','心形脸','菱形脸']
    skin_color: Color
    eye_shape: Literal['杏眼','圆眼','细长眼','上挑眼','下垂眼']
    eye_color: Color
    hair_shape: Phrase=Field(description='长度、轮廓、刘海、分缝、卷曲程度；具体且唯一')
    hair_color: Color
    features: list[Phrase]=Field(default_factory=list,max_length=3,description='固定可见特征，如左脸颊小圆痣；无特征填空数组')


class CreatureAppearance(Spec):
    form: Literal['类人神话生物','兽形神话生物','鸟形神话生物','蛇形神话生物','龙形神话生物','混合型神话生物','其他非人形生物']
    species: Phrase=Field(description='明确物种或神话身份，例如石猴、牛头人、九尾狐、中华龙；不得改写成人类')
    life_stage: Literal['幼体','少年体','青年体','成年体','老年体','不适用']
    presentation: Literal['女性外观','男性外观','中性外观','无性别外观']
    height_cm: float=Field(ge=5,le=10000,allow_inf_nan=False,description='直立高度或肩高，单位厘米')
    body_length_cm: float|None=Field(default=None,ge=5,le=50000,allow_inf_nan=False,description='非人形主体从头至尾的体长，单位厘米')
    wingspan_cm: float|None=Field(default=None,ge=5,le=50000,allow_inf_nan=False,description='有翼主体的翼展，单位厘米')
    body_structure: Phrase=Field(description='躯干比例、脊柱姿态及身体分段，例如直立类人骨架、宽肩牛躯与反关节后腿')
    head_structure: Phrase=Field(description='明确头部、口鼻、耳、角或喙的固定结构')
    body_surface: Phrase=Field(description='皮肤、毛发、鳞片、羽毛、甲壳或能量体的覆盖方式与质地')
    primary_color: Color
    eye_shape: Phrase=Field(description='眼睛数量、形状与瞳孔结构')
    eye_color: Color
    limbs: Phrase=Field(description='四肢数量、类型、末端结构与固定比例')
    features: list[Phrase]=Field(default_factory=list,max_length=6,description='尾、翼、角、鬃毛、纹路等不可丢失的身份锚点')


Appearance=HumanAppearance|CreatureAppearance


class Garment(Spec):
    slot: Literal['外套','上装','下装','全身服装','鞋履','配饰']
    category: Phrase=Field(description='具体服饰品类，例如双排扣短风衣')
    color: Color
    material: Material
    cut: Phrase=Field(description='领型、袖型、廓形、衣长等明确裁剪信息')
    details: list[Phrase]=Field(default_factory=list,max_length=2)


class AssetBase(Spec):
    name: str=Field(min_length=1,max_length=100,description='仅用于列表标识，不进入生图提示词')
    source_ref: str=Field(min_length=1,max_length=500)
    facts: str=Field(min_length=2,max_length=1000,description='原文依据，只存档，不进入生图提示词或设定描述')
    adaptation_notes: str=Field(default='',max_length=800,description='原文缺失的属性采用了哪些具体补充设计；只存档')


class CharacterSheet(AssetBase):
    role: Literal['character']
    character_id: str=Field(pattern=r'^C[0-9]{3}$',description='同一人物唯一身份编号；不能按服装拆分人物')
    appearance: Appearance
    costume_mode: Literal['required','optional','none']=Field(default='required',description='required 必须另建服装，optional 仅在片段明确需要时建立，none 不为该生物添加服装')

    @model_validator(mode='after')
    def identity_not_costume(self):
        if re.search(r'版本|[（(].*(?:裙|服装|着装).*[）)]|(?:红裙|白裙)版',self.name):
            raise ValueError('人物名称不能按服装划分版本，请把衣物建立为独立服装并绑定同一身份')
        return self


class CostumeSheet(AssetBase):
    role: Literal['costume']
    costume_id: str=Field(pattern=r'^W[0-9]{3}$')
    character_ref: str=Field(pattern=r'^C[0-9]{3}$',description='绑定已有唯一人物身份编号')
    wardrobe: list[Garment]=Field(min_length=1,max_length=5,description='同一套定装中的服饰部件，不得混入服装备选方案')


class Dimensions(Spec):
    width_m: float=Field(gt=0,allow_inf_nan=False,strict=True,description='空间或建筑的实际宽度，单位米')
    depth_m: float=Field(gt=0,allow_inf_nan=False,strict=True,description='空间或建筑的实际进深，单位米')
    height_m: float=Field(gt=0,allow_inf_nan=False,strict=True,description='单位米；室内为净高，建筑外景为总高，可超过100米')


class Surface(Spec):
    part: Literal['地面','墙面','天花板','立柱']
    material: Material
    color: Color
    finish: Finish


class SpatialObject(Spec):
    category: ObjectCategory=Field(description='具体的建筑构件、家具或静物名称；允许床、门、窗等单字中文实体名，不包含人物或界面')
    position: Phrase=Field(description='确定的方位与距离，例如北墙居中、距西墙1米')
    material: Material
    color: Color
    shape: Phrase=Field(description='明确的几何形状与尺寸')


class Lighting(Spec):
    source: LightSource
    direction: LightDirection
    temperature_k: int=Field(ge=1500,le=12000)
    softness: Literal['硬光','柔光','漫射光']
    intensity: Literal['低照度','中等照度','高照度']


class SceneSheet(AssetBase):
    role: Literal['scene']
    space_type: Phrase=Field(description='真实物理空间的具体类型，如公寓客厅、室内走廊')
    dimensions: Dimensions
    surfaces: list[Surface]=Field(min_length=2,max_length=4)
    layout: list[SpatialObject]=Field(min_length=1,max_length=6,description='门窗、出入口、家具及关键静物；同一对象只列一次')
    lighting: Lighting


def style_prompt(style):
    style=VisualStyle.model_validate(style)
    return '\n'.join([
        '绘画媒介：'+style.medium,'线稿工艺：'+style.linework,'明暗技法：'+style.shading,
        '整体配色比例（仅用于画面着色，不覆盖对象固有色，不得绘制成可见色卡）：'+'；'.join(f'{c.role} {c.hex.upper()} {c.percent}%' for c in style.palette),
        '饱和度：'+style.saturation,'明度关系：'+style.contrast,
        '边缘处理：'+style.edge_treatment,'画面肌理：'+style.surface_texture,
    ])


RENDERING_FIELDS={'medium':'绘画媒介','linework':'线稿工艺','shading':'明暗技法',
                  'contrast':'明度关系','edge_treatment':'边缘处理','surface_texture':'画面肌理'}


def rendering_style(style):
    """Keep rendering technique independent of palette and global saturation."""
    validated=VisualStyle.model_validate(style)
    return {key:getattr(validated,key) for key in RENDERING_FIELDS}


def rendering_prompt(rendering):
    return '\n'.join(label+'：'+rendering[key] for key,label in RENDERING_FIELDS.items())


def description(item):
    if item.role=='character':
        a=item.appearance
        if isinstance(a,CreatureAppearance):
            dimensions=f'高度{a.height_cm:g}厘米'
            if a.body_length_cm is not None:dimensions+=f'，体长{a.body_length_cm:g}厘米'
            if a.wingspan_cm is not None:dimensions+=f'，翼展{a.wingspan_cm:g}厘米'
            lines=[f'物种与形态：{a.species}，{a.form}，{a.life_stage}，{a.presentation}',
                f'尺度与身体结构：{dimensions}；{a.body_structure}',
                f'头部与眼睛：{a.head_structure}；{a.eye_shape}，瞳色{a.eye_color}',
                f'体表：{a.body_surface}；主固有色{a.primary_color}',
                f'肢体：{a.limbs}']
        else:
            lines=[f'外貌：{a.age_band}，{a.presentation}，身高{a.height_cm}厘米，{a.head_body_ratio:g}头身，{a.build}',
                f'头面部：{a.face_shape}，肤色{a.skin_color}，{a.eye_shape}，瞳色{a.eye_color}',
                f'发型：{a.hair_shape}；发色{a.hair_color}']
        if a.features:lines.append('固定特征：'+'；'.join(a.features))
        return '\n'.join(lines)
    if item.role=='costume':
        return '\n'.join(f'{g.slot}：{g.category}，{g.color}，{g.material}，{g.cut}'+('，'+'；'.join(g.details) if g.details else '') for g in item.wardrobe)
    d=item.dimensions;l=item.lighting
    return '\n'.join([
        f'空间：{item.space_type}；宽{d.width_m:g}米×深{d.depth_m:g}米×高{d.height_m:g}米',
        *[f'{s.part}：{s.material}，{s.color}，{s.finish}' for s in item.surfaces],
        *[f'布局：{o.category}；{o.position}；{o.material}，{o.color}；{o.shape}' for o in item.layout],
        f'光照：{l.source}，{l.direction}，{l.temperature_k}K，{l.softness}，{l.intensity}',
    ])


def validate_description_budget(item):
    if len(description(item))>1400:raise ValueError('静态规格过长，请精简属性项，避免超出后续资产说明限制')
    return item
