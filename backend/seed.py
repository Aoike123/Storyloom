from .db import Record, Session
from .domain import JOIN_STATE

STORY = {
    'title': '雨夜最后一班车', 'subtitle': '有些结局注定抵达，过程由你改写。',
    'genre': '悬疑 · 都市', 'duration': 60,
    'synopsis': '深夜，记者林夏在废弃站台追查一份失踪账本。长椅下的纸袋、连通值班室的紧急按钮，以及突然出现的陌生人，将她推向一个必须亲自作出的选择。',
    'ending': '林夏活着向值班室提交真实账本，证据得到保存，案件真相公开。',
    'constraints': ['没有超自然能力；人物行动受现场条件限制', '已经发生的事实不可被静默撤销', '账本真实存在于长椅下，必须先找到再提交', '林夏最终活着提交证据，案件真相公开'],
    'status': 'demo_published', 'source': '本项目原创技术演示；视频为程序绘制的示意动画，不是模型生成漫剧。',
    'original_ids': ['clip_open', 'clip_turn', 'clip_end'],
}
CLIPS = [
    {'id':'clip_open','title':'01 · 站台来客','duration':20,'scene':'platform','media':'/media/clip_open.mp4',
     'narration':'雨水敲打站台顶棚。林夏注意到长椅下的纸袋，立柱旁有一枚紧急求助按钮。陌生人的脚步停在她身后。',
     'events':[], 'entry_state':{}, 'status':'approved','demo':True, 'assets':['asset_lin','asset_stranger','asset_platform']},
    {'id':'clip_turn','title':'02 · 危险与证据','duration':24,'scene':'platform','media':'/media/clip_turn.mp4',
     'narration':'刀光划破雨幕。林夏绕过长椅按下求助按钮，值班员赶来收存刀具。她在长椅下找到真实账本，带到值班室提交。',
     'events':[{'at':6,'text':'林夏按下紧急按钮','changes':{'help':'called'}},
               {'at':12,'text':'值班员赶来收存刀具','changes':{'help':'present','knife':'secured'}},
               {'at':17,'text':'林夏找到长椅下的账本','changes':{'evidence':'found'}},
               {'at':22,'text':'林夏进入值班室提交账本','changes':{'location':'security','evidence':'submitted'}}],
     'entry_state':{},'status':'approved','demo':True,'assets':['asset_lin','asset_stranger','asset_platform']},
    {'id':'clip_end','title':'03 · 黎明的回声','duration':16,'scene':'security','media':'/media/clip_end.mp4',
     'narration':'账本被收进证物袋。窗外天色渐亮，林夏的报道将这段雨夜留在公众视线中。真相终于有了回声。',
     'events':[{'at':10,'text':'证据公开，案件真相得到揭示','changes':{'case':'resolved'}}],
     'entry_state':JOIN_STATE, 'status':'approved','demo':True,'assets':['asset_lin','asset_security']},
]
ASSETS = [
 {'id':'asset_lin','name':'林夏','type':'character','description':'27 岁调查记者，短发，米白风衣，沉着敏锐。','palette':'cream','status':'approved','mutable':['description'],'media':'','revision_note':'演示角色设定'},
 {'id':'asset_stranger','name':'陌生人','type':'character','description':'深色连帽外套，试图阻止账本曝光；没有超自然能力。','palette':'ink','status':'approved','mutable':['description'],'media':'','revision_note':'演示角色设定'},
 {'id':'asset_platform','name':'旧城站台','type':'scene','description':'雨夜，长椅与立柱，长椅下有账本；紧急按钮连通上层值班室。','palette':'rain','status':'approved','mutable':['description'],'media':'','revision_note':'演示场景设定'},
 {'id':'asset_security','name':'车站值班室','type':'scene','description':'暖色灯光，证物袋，窗外天色渐亮；用于提交账本与尾声。','palette':'amber','status':'approved','mutable':['description'],'media':'','revision_note':'演示场景设定'},
]

def seed():
    with Session.begin() as db:
        if db.get(Record, 'story_demo'):
            return
        db.add(Record(id='story_demo', kind='story', data=STORY))
        db.add(Record(id='paid_budget',kind='budget',data={'used':0}))
        for clip in CLIPS:
            db.add(Record(id=clip['id'],kind='clip',data={k:v for k,v in clip.items() if k!='id'}))
        for asset in ASSETS:
            db.add(Record(id=asset['id'],kind='asset',data={k:v for k,v in asset.items() if k!='id'}))
        db.add(Record(id='audit_seed',kind='audit',data={'target':'story_demo','action':'seed_demo','note':'内置示例已预设状态，仅用于功能演示，不代表用户人工审核。'}))
