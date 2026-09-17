def style():
    return {'medium':'二维数字蘸水笔勾线','linework':'外轮廓粗线、内部结构细线、笔压粗细变化',
        'shading':'两阶硬边赛璐璐阴影','palette':[
            {'role':'主色','hex':'#29363D','percent':60},{'role':'辅色','hex':'#B6B9AA','percent':30},{'role':'点缀色','hex':'#A82E37','percent':10}],
        'saturation':'低饱和度','contrast':'高明度对比、暗部保留层次','edge_treatment':'锐利清晰边缘','surface_texture':'细颗粒绘图纸纹理'}


def character():
    return {'role':'character','character_id':'C001','name':'女主','source_ref':'P001','facts':'原文中的第一人称人物',
        'appearance':{'age_band':'青年成人','presentation':'女性外观','height_cm':165,'head_body_ratio':7,
            'build':'标准体型','face_shape':'椭圆脸','skin_color':'#DFC4AB','eye_shape':'杏眼','eye_color':'#382C24',
            'hair_shape':'齐耳直短发，偏左分缝','hair_color':'#191C20','features':['左脸颊小圆痣']}}


def costume(cid='W001',color='#9A2438'):
    return {'role':'costume','costume_id':cid,'character_ref':'C001','name':'连衣裙'+cid,'source_ref':'P001','facts':'原文提及连衣裙',
        'wardrobe':[{'slot':'全身服装','category':'圆领长袖连衣裙','color':color,'material':'棉布',
            'cut':'收腰，及膝，直筒长袖','details':['左侧衣领圆形胸针']}]}


def bare_costume(costume_id='W001',character_ref='C002'):
    """The empty-clothing mode: the character keeps the costume stage but wears nothing."""
    return {'role':'costume','costume_id':costume_id,'character_ref':character_ref,'name':'天然体表'+costume_id,
        'source_ref':'P001','facts':'原文说明该角色为天然体表、不着衣物','mode':'bare',
        'bare_surface':'自然体表与形体特征完全来自人物身份图，不添加任何衣物、盔甲、法器或饰品'}


def scene():
    return {'role':'scene','name':'客厅','source_ref':'P001','facts':'原文中的房间','space_type':'公寓客厅',
        'dimensions':{'width_m':6,'depth_m':4,'height_m':3},
        'surfaces':[{'part':'地面','material':'瓷砖','color':'#B1B2A5','finish':'半哑光'},
                    {'part':'墙面','material':'灰泥','color':'#D1CEB9','finish':'哑光'}],
        'layout':[{'category':'单扇入户门','position':'北墙偏左，距西墙1米','material':'钢材','color':'#434C45','shape':'宽1米高2米矩形'}],
        'lighting':{'source':'顶置灯板','direction':'顶部向下','temperature_k':4500,'softness':'柔光','intensity':'中等照度'}}


def plan():return {'visual_style':style(),'items':[character(),costume(),scene()]}


def design_response(system,payload,*args,**kwargs):
    if payload['schema']['title']=='StylePlan':return {'visual_style':style()},{}
    if payload['schema']['title']=='CharacterPlan':return {'characters':[character()]},{}
    if payload['schema']['title']=='CostumePlan':return {'costumes':[costume()]},{}
    if payload['schema']['title']=='ScenePlan':return {'scenes':[scene()]},{}
    if payload['schema']['title']=='AssetPromptBatch':return {'items':[{'asset_index':a['asset_index'],'prompt':a['render_contract']} for a in payload['assets']]},{}
    if payload['schema']['title']=='IdentityPlan':return {'visual_style':style(),'characters':[character()]},{}
    if payload['schema']['title']=='WardrobeScenePlan':return {'items':[costume(),scene()]},{}
    raise AssertionError('Unexpected design stage: '+payload['schema']['title'])
