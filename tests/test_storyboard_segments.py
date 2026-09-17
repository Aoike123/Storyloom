"""切割节点与逐情节分镜：每个情节一次调用、一套自己的镜号。"""
import copy
import pytest
from backend import director as d,segments
from test_director import segment_plan


def long_text():
    pieces=['夜色压在公司楼顶，方诺把手机贴在耳边，听见电流一样的杂音。','她数了三遍楼梯，还是没有找到那扇该开的门。',
            '洗手间的灯忽明忽暗，镜子里的人比现实慢了半拍。','方诺伸手去碰镜面，指尖穿过冰凉的水汽。',
            '走廊尽头的门被推开，护士推着空床经过，床单上有一道新鲜的折痕。','她终于明白，今晚的医院只收留记得住自己名字的人。']
    return ''.join(pieces)*5


def treatment():
    text={key:'明确的阐述依据' for key in ('premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy')}
    text.update(rules=[{'rule':'镜子慢半拍','quote':'镜子里的人比现实慢了半拍','consequence':'人物开始怀疑自己'}],boundaries=['结局未知'])
    return d.Treatment.model_validate(text)


def shot(shot_id,quote,reference,extra=None):
    item={field:'明确的镜头设计说明' for field in ('scene','dramatic_action','camera','composition','blocking','continuity_in',
        'continuity_out','viewer_knows','character_knows','withhold','sound','transition','reference_prompt','motion_prompt','generation_risk')}
    item.update(id=shot_id,purpose='setup',source_ref=reference,source_quote=quote,size='MS',setup_ids=[],
        edit_seconds=3,generation_seconds=5,dialogue='',assets=['女主'])
    if extra:item.update(extra)
    return item


def two_segment_case(shot_budget=3):
    """两个情节的原文、切割结果与各自的分镜；镜号在每个情节内重新开始。"""
    text=long_text();passages=d.source_passages(text)
    plan=segment_plan((('P001','P002'),('P003','P004')),shot_budget=shot_budget)
    first=segments.segment_text(plan['segments'][0],passages)
    second=segments.segment_text(plan['segments'][1],passages)
    chunks={'G01':{'title':'片段一','scope_note':'第一片段','shots':[
            shot('G01-S01',first[:20],'P001'),shot('G01-S02',first[20:40],'P002'),
            shot('G01-S03',first[40:60],'P002',{'continuity_out':'人物停在镜面前'})]},
        'G02':{'title':'片段二','scope_note':'第二片段','shots':[
            shot('G02-S01',second[:20],'P003',{'purpose':'reveal','setup_ids':[],'continuity_in':'镜面异常仍未解释'}),
            shot('G02-S02',second[20:40],'P004')]}}
    return text,passages,plan,first,second,chunks


def test_segment_plan_requires_contiguous_original_text():
    passages=d.source_passages(long_text())
    assert list(passages)[:3]==['P001','P002','P003']
    plan=segments.SegmentPlan.model_validate(segment_plan((('P001','P002'),('P003',))))
    assert segments.plan_issues(plan,passages)==[]
    gap=segments.SegmentPlan.model_validate(segment_plan((('P001',),('P003',))))
    # 漏段与重复覆盖分别给出可执行的修正指令，而不是笼统的"必须连续"。
    gap_issues=segments.plan_issues(gap,passages)
    assert len(gap_issues)==1 and '漏掉了原文' in gap_issues[0],gap_issues
    assert 'P002 没有归属' in gap_issues[0] and '必须从 P002 开始' in gap_issues[0]
    overlap=segments.SegmentPlan.model_validate(segment_plan((('P001','P002'),('P002','P003'))))
    overlap_issues=segments.plan_issues(overlap,passages)
    assert len(overlap_issues)==1 and '重复覆盖' in overlap_issues[0]
    assert '必须从 P003 开始' in overlap_issues[0] and '不要列出 P002' in overlap_issues[0]
    unknown=segments.SegmentPlan.model_validate(segment_plan((('P001','P099'),)))
    assert any('不存在的原文编号 P099' in issue for issue in segments.plan_issues(unknown,passages))
    crowded=segments.SegmentPlan.model_validate(segment_plan(tuple((f'P{index:03}',) for index in range(1,6)),shot_budget=6))
    assert any('超过当前上限' in issue for issue in segments.plan_issues(crowded,passages))
    misnumbered=copy.deepcopy(segment_plan((('P001',),)))
    misnumbered['segments'][0]['id']='G02'
    assert any('连续递增' in issue for issue in segments.plan_issues(segments.SegmentPlan.model_validate(misnumbered),passages))


def test_cutting_node_retries_until_every_passage_is_covered(monkeypatch):
    text=long_text();passages=d.source_passages(text)
    plan=segment_plan((('P001','P002'),('P003','P004')),shot_budget=3)
    skipped=copy.deepcopy(plan);skipped['segments'][1]['source_refs']=['P004']
    answers=iter([skipped,plan]);systems=[];saved={}
    def chat(system,payload,*a,**kwargs):
        systems.append(system);return next(answers),{}
    monkeypatch.setattr(d,'chat_json',chat)
    _,cut,origin=d.segment_stage({'source':{'content':text},'brief':'测试'},passages,'cut-task',
        lambda key,value,progress:saved.update({key:value}),treatment())
    assert origin=='generated'
    assert '漏掉了原文' in systems[1] and '必须从 P003 开始' in systems[1]
    assert [segment['source_refs'] for segment in cut['segments']]==[['P001','P002'],['P003','P004']]
    assert cut['shot_total']==6
    assert cut['segments'][0]['source_text'].startswith('夜色压在')
    assert cut['fingerprint'] and len(cut['fingerprint'])==32
    assert '漏掉了原文' in saved['segment_diagnostics']['attempts'][0]['error']


def review_answer():
    return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}


def plan_episode(monkeypatch,text,plan,chunks,segment_id,unit_boards=None,prompts=True):
    """Run one episode's storyboard task; returns the payloads the model saw and the saved keys."""
    payloads=[];saved={}
    def node(_chat,name,payload,*args,**kwargs):
        if name=='storyboard':
            payloads.append(payload);return kwargs['validator'](chunks[payload['segment']['id']]),{}
        if name=='shot_prompts':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return review_answer(),{}
    monkeypatch.setattr(d,'call_node',node)
    task={'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
          'segments':plan,'segment_id':segment_id,'professional_prompts':prompts}
    if unit_boards:task['unit_boards']=unit_boards
    d.run_director(task,f'node-{segment_id}',lambda key,value,progress:saved.update({key:value}))
    return payloads,saved


def test_each_episode_is_planned_on_its_own_timeline(monkeypatch):
    text,_,plan,_,_,chunks=two_segment_case()
    first_payloads,first_saved=plan_episode(monkeypatch,text,plan,chunks,'G01')
    assert [item['segment']['shot_ids'] for item in first_payloads]==[['G01-S01','G01-S02','G01-S03']]
    assert first_payloads[0]['previous_segment'] is None and first_payloads[0]['next_segment']['segment_id']=='G02'
    # 第一个情节完成后，合并片只有这一段，镜号就是本情节的镜号。
    assert [item['id'] for item in first_saved['board']['shots']]==['G01-S01','G01-S02','G01-S03']
    assert first_saved['board']['scope_note'].startswith('按情节顺序合并；已完成 G01')
    second_payloads,second_saved=plan_episode(monkeypatch,text,plan,chunks,'G02',first_saved['units'])
    # 第二个情节从 S01 重新开始编号，只能引用第一个情节里真实存在的镜号。
    # 允许的镜号是本情节预算内的编号，从 G02-S01 起；实际用几镜由模型决定。
    assert second_payloads[0]['segment']['shot_ids'][:2]==['G02-S01','G02-S02']
    assert all(item.startswith('G02-S') for item in second_payloads[0]['segment']['shot_ids'])
    assert second_payloads[0]['segment']['referenceable_shot_ids']==['G01-S01','G01-S02','G01-S03']
    assert second_payloads[0]['previous_segment']['last_shot_continuity_out']=='人物停在镜面前'
    shots=second_saved['board']['shots']
    assert [item['id'] for item in shots]==['G01-S01','G01-S02','G01-S03','G02-S01','G02-S02']
    assert shots[3]['continuity_in'].startswith('承接 G01-S03（上一片段结尾）')
    assert shots[3]['setup_ids']==['G01-S03'] and shots[3]['purpose']=='reveal'
    assert second_saved['board']['scope_note'].startswith('按情节顺序合并；已完成 G01、G02')
    assert second_saved['board']['shots'][0]['reference_prompt']=='静态镜头参考图提示词'


def test_only_the_episode_being_retried_is_planned_again(monkeypatch):
    """重试一个情节不会再付一次前面情节的钱。"""
    text,_,plan,_,_,chunks=two_segment_case()
    _,first_saved=plan_episode(monkeypatch,text,plan,chunks,'G01')
    payloads,_=plan_episode(monkeypatch,text,plan,chunks,'G02',first_saved['units'])
    assert [item['segment']['id'] for item in payloads]==['G02']
