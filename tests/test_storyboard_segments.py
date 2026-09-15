"""微小说切割节点与分块分区生成分镜的聚焦测试。"""
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


def shot(index,quote,reference,extra=None):
    item={field:'明确的镜头设计说明' for field in ('scene','dramatic_action','camera','composition','blocking','continuity_in',
        'continuity_out','viewer_knows','character_knows','withhold','sound','transition','reference_prompt','motion_prompt','generation_risk')}
    item.update(id=f'S{index:02}',purpose='setup',source_ref=reference,source_quote=quote,size='MS',setup_ids=[],
        edit_seconds=3,generation_seconds=5,dialogue='',assets=['女主'])
    if extra:item.update(extra)
    return item


def two_segment_case(shot_budget=3):
    text=long_text();passages=d.source_passages(text)
    plan=segment_plan((('P001','P002'),('P003','P004')),shot_budget=shot_budget)
    first=segments.segment_text(plan['segments'][0],passages)
    second=segments.segment_text(plan['segments'][1],passages)
    # Shots are numbered on the whole-film timeline: the second segment continues at S04.
    start=shot_budget+1
    chunks={'G01':{'title':'片段一','scope_note':'第一片段','shots':[
            shot(1,first[:20],'P001'),shot(2,first[20:40],'P002'),
            shot(3,first[40:60],'P002',{'continuity_out':'人物停在镜面前'})]},
        'G02':{'title':'片段二','scope_note':'第二片段','shots':[
            shot(start,second[:20],'P003',{'purpose':'reveal','setup_ids':[],'continuity_in':'镜面异常仍未解释'}),
            shot(start+1,second[20:40],'P004')]}}
    return text,passages,plan,first,second,chunks


def test_segment_plan_requires_contiguous_original_text():
    passages=d.source_passages(long_text())
    assert list(passages)[:3]==['P001','P002','P003']
    plan=segments.SegmentPlan.model_validate(segment_plan((('P001','P002'),('P003',))))
    assert segments.plan_issues(plan,passages)==[]
    gap=segments.SegmentPlan.model_validate(segment_plan((('P001',),('P003',))))
    assert any('必须连续' in issue for issue in segments.plan_issues(gap,passages))
    overlap=segments.SegmentPlan.model_validate(segment_plan((('P001','P002'),('P002','P003'))))
    assert any('必须连续' in issue for issue in segments.plan_issues(overlap,passages))
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
    assert '必须连续' in systems[1]
    assert [segment['source_refs'] for segment in cut['segments']]==[['P001','P002'],['P003','P004']]
    assert cut['shot_total']==6 and cut['segments'][1]['shot_start']==4
    assert cut['segments'][0]['source_text'].startswith('夜色压在')
    assert cut['fingerprint'] and len(cut['fingerprint'])==32
    assert '必须连续' in saved['segment_diagnostics']['attempts'][0]['error']


def test_generation_is_partitioned_per_segment_and_merged_with_global_shot_numbers(monkeypatch):
    text,passages,plan,_,_,chunks=two_segment_case()
    payloads=[];saved={}
    def node(_chat,name,payload,*args,**kwargs):
        if name=='storyboard':
            payloads.append(payload);return kwargs['validator'](chunks[payload['segment']['id']]),{}
        if name=='shot_prompts':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}
    monkeypatch.setattr(d,'call_node',node)
    result=d.run_director({'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
                           'segments':plan,'professional_prompts':True},'chunk-node',lambda key,value,progress:saved.update({key:value}))
    assert result['approved']
    assert [item['segment']['id'] for item in payloads]==['G01','G02']
    assert [sorted(item['source_passages']) for item in payloads]==[['P001','P002'],['P003','P004']]
    assert [item['schema']['properties']['shots']['maxItems'] for item in payloads]==[3,3]
    assert [item['segment']['shot_start'] for item in payloads]==[1,4]
    assert payloads[0]['previous_segment'] is None and payloads[0]['next_segment']['segment_id']=='G02'
    assert payloads[1]['previous_segment']['last_shot_continuity_out']=='人物停在镜面前'
    assert len(payloads[1]['segment_outline'])==1 and payloads[1]['segment_outline'][0]['id']=='G01'
    shots=saved['board']['shots']
    assert [item['id'] for item in shots]==['S01','S02','S03','S04','S05']
    assert [item['segment_id'] for item in shots]==['G01','G01','G01','G02','G02']
    assert shots[3]['continuity_in'].startswith('承接 S03（上一片段结尾）')
    assert shots[3]['setup_ids']==['S03'] and shots[3]['purpose']=='reveal'
    assert saved['board']['scope_note'].startswith('按微小说切割出的 2 个片段分块生成')
    assert saved['board_plan']['shots'][0]['reference_prompt']=='明确的镜头设计说明'
    assert saved['board']['shots'][0]['reference_prompt']=='静态镜头参考图提示词'


def test_cutting_stays_first_and_every_segment_gets_its_own_model_call(monkeypatch):
    text,_,plan,_,_,chunks=two_segment_case()
    seen=[];saved={}
    def chat(system,payload,*args,**kwargs):
        kind=payload['schema']['title'];seen.append(kind)
        if kind=='SegmentPlan':return plan,{}
        if kind=='Board':return chunks[payload['segment']['id']],{}
        if kind=='ShotPromptBatch':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}
    monkeypatch.setattr(d,'chat_json',chat)
    d.run_director({'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
                    'professional_prompts':True},'order-node',lambda key,value,progress:saved.update({key:value}))
    assert seen==['SegmentPlan','Board','Board','ShotPromptBatch','ShotPromptBatch','Review']
    assert saved['segments']['segments'][0]['id']=='G01' and saved['segments']['shot_total']==6


def test_segment_scoped_retry_reuses_already_validated_segments(monkeypatch):
    """A retry after one bad segment must not repay for the segments that already passed."""
    text,passages,plan,_,_,chunks=two_segment_case()
    calls=[]
    def node(_chat,name,payload,*args,**kwargs):
        calls.append((name,payload.get('segment',{}).get('id')))
        if name=='storyboard':return kwargs['validator'](chunks[payload['segment']['id']]),{}
        if name=='shot_prompts':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}  # noqa: E501
    monkeypatch.setattr(d,'call_node',node)
    request={'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
             'segments':plan,'professional_prompts':True}
    saved={}
    d.run_director(request,'first-pass',lambda key,value,progress:saved.setdefault(key,value) if key=='board_progress' else None)
    first=len([call for call in calls if call[0]=='storyboard'])
    assert first==2

    saved_progress=saved['board_progress']
    # Segment G02 fails on the retry; G01 must still be reused instead of regenerated.
    def node_retry(_chat,name,payload,*args,**kwargs):
        calls.append((name,payload.get('segment',{}).get('id')))
        if name=='storyboard':
            return kwargs['validator'](chunks[payload['segment']['id']]),{}
        if name=='shot_prompts':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}  # noqa: E501
    monkeypatch.setattr(d,'call_node',node_retry)
    calls.clear()
    retry={**request,'board_progress':saved_progress,'retry_feedback':'G02 上一次分镜输出问题','retry_reuse_chunks':True}
    d.run_director(retry,'segment-retry',lambda key,value,progress:None)
    storyboard_calls=[call for call in calls if call[0]=='storyboard']
    assert [call[1] for call in storyboard_calls]==['G02']


def test_whole_board_retry_still_regenerates_every_segment(monkeypatch):
    text,_,plan,_,_,chunks=two_segment_case()
    calls=[]
    def node(_chat,name,payload,*args,**kwargs):
        calls.append((name,payload.get('segment',{}).get('id')))
        if name=='storyboard':return kwargs['validator'](chunks[payload['segment']['id']]),{}
        if name=='shot_prompts':
            return {'shots':[{'id':item['id'],'reference_prompt':'静态镜头参考图提示词','motion_prompt':'单镜运动提示词'}
                             for item in payload['board']['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}  # noqa: E501
    monkeypatch.setattr(d,'call_node',node)
    saved={}
    request={'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
             'segments':plan,'professional_prompts':True}
    d.run_director(request,'first-pass',lambda key,value,progress:saved.setdefault(key,value) if key=='board_progress' else None)
    calls.clear()
    retry={**request,'board_progress':saved['board_progress'],'retry_feedback':'整片分镜结构不合格'}
    d.run_director(retry,'whole-retry',lambda key,value,progress:None)
    assert [call[1] for call in calls if call[0]=='storyboard']==['G01','G02']


def test_validated_chunks_are_reused_instead_of_calling_the_model_again(monkeypatch):
    text,passages,plan,first,second,_=two_segment_case(shot_budget=2)
    saved_chunk={'title':'片段一','scope_note':'第一片段','shots':[shot(1,first[:20],'P001'),shot(2,first[20:40],'P002')]}
    fresh_chunk={'title':'片段二','scope_note':'第二片段','shots':[shot(3,second[:20],'P003'),shot(4,second[20:40],'P004')]}
    plan_object=segments.plan_from_saved(plan)
    progress={'fingerprint':segments.plan_fingerprint(plan_object,passages),'preproduction_stamp':None,
              'segments':segments.plan_dump(plan_object,passages)['segments'],'chunks':{'G01':saved_chunk}}
    calls=[];saved={}
    def node(_chat,name,payload,*args,**kwargs):
        calls.append(name)
        if name=='storyboard':return kwargs['validator'](copy.deepcopy(fresh_chunk)),{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}
    monkeypatch.setattr(d,'call_node',node)
    d.run_director({'stage':'board','source':{'content':text},'brief':'测试','treatment':treatment().model_dump(),
                    'segments':plan,'board_progress':progress},'reuse-node',lambda key,value,progress:saved.update({key:value}))
    assert calls.count('storyboard')==1
    assert saved['board_chunk_reuse']['segment_ids']==['G01']
    assert saved['board_progress']['reused']==['G01']
    assert [item['id'] for item in saved['board']['shots']]==['S01','S02','S03','S04']


def board_from(chunk):
    from backend.director import Board
    return Board.model_validate(chunk)


def test_cross_segment_setup_reference_keeps_its_whole_film_meaning():
    """A link back to the opening must not be relabelled into this segment's own first shot."""
    _,_,plan,_,_,chunks=two_segment_case()
    chunks['G02']['shots'][1]['purpose']='payoff'
    chunks['G02']['shots'][1]['setup_ids']=['S01']
    merged=segments.merge_chunks(segments.plan_from_saved(plan),[board_from(chunks['G01']),board_from(chunks['G02'])])
    assert [shot.id for shot in merged]==['S01','S02','S03','S04','S05']
    assert merged[4].setup_ids==['S01']


def test_segment_that_restarts_numbering_is_rejected_instead_of_relabelled():
    _,_,plan,_,_,chunks=two_segment_case()
    chunks['G02']['shots'][0]['id']='S01'
    chunks['G02']['shots'][1]['id']='S02'
    with pytest.raises(segments.NumberingError,match='S04'):
        segments.merge_chunks(segments.plan_from_saved(plan),[board_from(chunks['G01']),board_from(chunks['G02'])])
