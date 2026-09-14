"""Explicit, versioned workflow-node bindings to vendored professional skills."""
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from string import Template
from .db import Record,Task,Session
from .providers import ProviderError

ROOT=Path(__file__).with_name('node_skills')


def _read(path):
    target=(ROOT/path).resolve()
    if not target.is_relative_to(ROOT.resolve()):raise ProviderError('Skill 路径无效。')
    return target.read_text(encoding='utf-8')


def _section(text,heading):
    match=re.search(r'^## '+re.escape(heading)+r'\s*\n(.*?)(?=^## |\Z)',text,re.M|re.S)
    if not match:raise ProviderError('Skill 缺少已绑定章节：'+heading)
    return match.group(1).strip()


def snapshot(node):
    registry=json.loads(_read('registry.json'))
    if node not in registry['nodes']:raise ProviderError('工作流节点没有绑定专业 Skill：'+node)
    config=registry['nodes'][node];adapter=_read(config['adapter'])
    lock=json.loads(_read('vendor-lock.json'));sections=[];sources=[]
    for source in config['sources']:
        package=lock['sources'][source['bundle']]
        path='skills/'+source['skill']+'/SKILL.md';text=_read('vendor/'+source['bundle']+'/'+path)
        if hashlib.sha256(text.encode()).hexdigest()!=package['files'][path]:raise ProviderError('已锁定的 Skill 源文件发生变化：'+source['skill'])
        sections.append('\n'.join(_section(text,h) for h in source['sections']))
        reference='skills/'+source['skill']+'/references/field-guide.md'
        if reference in package['files']:
            guide=_read('vendor/'+source['bundle']+'/'+reference)
            if hashlib.sha256(guide.encode()).hexdigest()!=package['files'][reference]:raise ProviderError('Skill 专业参考文件已变化。')
            sections.append(guide)
        sources.append({'name':source['skill'],'repo':package['repo'],'commit':package['commit'],
            'license':package['license'],'sections':source['sections'],'url':'https://github.com/'+package['repo']+'/blob/'+package['commit']+'/'+path})
    adapter_body=re.sub(r'^---\s*\n.*?\n---\s*\n','',adapter,count=1,flags=re.S)
    if config.get('local_sections'):
        contract=(Path(__file__).with_name('prompts')/'asset_visual_spec.md').read_text(encoding='utf-8')
        sections.append('\n'.join(_section(contract,h) for h in config['local_sections']))
    system=('你正在执行一个明确绑定的专业节点。以下上游 Skill 的专业方法供本节点使用。'
        '只执行本节点适配契约，输出调用方给定的 JSON Schema，不使用上游仓库的外层信封或工具命令。'
        '模型与供应商由项目配置指定；不检索、购买、切换模型，不调用外部工具。\n\n'
        +'\n\n'.join(sections)+'\n\n# 本项目节点适配契约\n'+adapter_body)
    return {'node':node,'title':config['title'],'version':registry['version'],'mode':config['mode'],
        'schema':config.get('schema'),'inputs':config.get('inputs'),'sources':sources,'adapter':config['adapter'],
        'sha256':hashlib.sha256(system.encode()).hexdigest(),'system':system,'adapter_text':adapter_body}


def public(binding):
    data={k:copy.deepcopy(binding[k]) for k in ('node','title','version','mode','sha256','sources','adapter')}
    if binding['mode']=='human':data['checklist']=re.findall(r'^- (.+)$',_section(binding['adapter_text'],'Checklist'),re.M)
    return data


def catalog():
    return [public(snapshot(node)) for node in json.loads(_read('registry.json'))['nodes']]


def _start(task_id,node):
    with Session.begin() as db:
        task=db.get(Task,task_id)
        if task is not None and task.status!='running':raise ProviderError('任务当前未运行，专业节点不会继续调用模型。')
        pins=task.payload.get('node_skill_pins',{}) if task else {}
        binding=pins.get(node) or snapshot(node)
        if not task:return binding,None
        task.payload={**task.payload,'node_skill_pins':{**pins,node:binding}}
        key='skill_trace_'+task_id;record=db.get(Record,key)
        calls=list(record.data['calls']) if record else []
        index=len(calls);calls.append({**public(binding),'status':'running','started_at':time.time()})
        if record:record.data={'calls':calls}
        else:db.add(Record(id=key,kind='node_skill_trace',data={'calls':calls}))
        return binding,index


def _finish(task_id,index,status):
    if index is None:return
    with Session.begin() as db:
        record=db.get(Record,'skill_trace_'+task_id)
        calls=copy.deepcopy(record.data['calls']);calls[index].update(status=status,finished_at=time.time())
        record.data={'calls':calls}


def call_node(chat,node,payload,task_id,profile='default',**kwargs):
    binding,index=_start(task_id,node)
    try:
        allowed=binding['inputs']
        if allowed is not None and set(payload)-set(allowed):raise ProviderError('节点收到越界输入：'+node)
        if binding['schema'] and payload.get('schema',{}).get('title')!=binding['schema']:
            raise ProviderError('节点输出格式与绑定契约不一致：'+node)
        digest=hashlib.sha256(json.dumps([task_id,node,binding['sha256'],payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        cache_id='node_output_'+digest[:48]
        with Session() as db:
            cached=db.get(Record,cache_id)
            if cached:
                value=(cached.data['response'],cached.data.get('usage',{}))
                _finish(task_id,index,'reused')
                return value
        value=chat(binding['system'],payload,task_id,profile,**kwargs)
        if index is not None:
            with Session.begin() as db:
                if not db.get(Record,cache_id):db.add(Record(id=cache_id,kind='node_output',data={
                    'task_id':task_id,'node':node,'input_sha256':digest,'response':value[0],'usage':value[1]}))
        _finish(task_id,index,'completed')
        return value
    except Exception:
        _finish(task_id,index,'failed')
        raise


def render_node(node,values):
    binding=snapshot(node)
    if binding['mode']!='template':raise ProviderError('该节点不是提示词模板：'+node)
    try:prompt=Template(_section(binding['adapter_text'],'Template')).substitute(values)
    except (KeyError,ValueError):raise ProviderError('提示词节点输入不完整：'+node) from None
    return {'prompt':prompt,'node_skill':public(binding)}
