from PIL import Image
from backend import creative as c,preproduction as prep
from backend.db import DATA,Record,Session,Task
from test_director import TEXT,recoverable_board,reference_prep


def test_author_resume_requeues_saved_storyboard_without_resubmitting_it(client,monkeypatch):
    raw=recoverable_board();references=reference_prep()
    raw['shots'][1].update(purpose='reveal',setup_ids=[],continuity_in='承接 S01 已建立的异常规则')
    monkeypatch.setattr(prep,'ready',lambda db,pid:references)
    with Session.begin() as db:
        db.add(Record(id='story-project',kind='director',data={
            'task_id':'story-child','board_diagnostics':{'raw':raw,'errors':[{'field':'assets','reason':'S02 duplicate'}]}}))
        db.add(Record(id='creative_story-project',kind='creative_run',data={
            'stage':'storyboarding','watch':'story-watch'}))
        db.add(Task(id='story-child',kind='director',status='needs_review',payload={
            'mode':'live','stage':'board','project_id':'story-project','preproduction':references,
            'production_phase':'storyboarding','production_node':'old-story-node','source':{'content':TEXT}}))
        db.add(Task(id='story-watch',kind='creative_watch',status='needs_review',payload={
            'mode':'live','creative_id':'story-project','child':'story-child',
            'production_phase':'storyboarding','production_node':'old-story-node'}))
        db.add(Task(id='old-story-node',kind='author_storyboard',status='needs_review',payload={
            'mode':'live','work_id':'story-work','run_id':'run-1','phase':'storyboarding'}))
        db.add(Record(id='story-work',kind='author_project',data={
            'director_id':'story-project','stage':'storyboarding','run_id':'run-1','supervisor':'old-story-node',
            'production_nodes':{'storyboarding':'old-story-node'}}))
    response=client.post('/api/author/projects/story-work/resume')
    assert response.status_code==200,response.text
    with Session() as db:
        child=db.get(Task,'story-child');watch=db.get(Task,'story-watch');work=db.get(Record,'story-work')
        replacement=db.get(Task,work.data['supervisor'])
        assert child.status==watch.status=='queued'
        assert child.payload['saved_board']['shots'][1]['setup_ids']==['S01']
        assert child.payload['production_node']==watch.payload['production_node']==replacement.id
        assert replacement.status=='queued' and replacement.payload['revision_of']=='old-story-node'
        assert db.get(Task,'old-story-node').status=='superseded'
        changes=db.get(Record,'story-project').data['board_recovery']['changes']
        assert any(change['action']=='bind_explicit_setup_reference' for change in changes)


def test_author_resume_conditionally_stitches_saved_over_limit_board_without_ai_call(client):
    from test_director import board
    media=DATA/'media'/'recovery-reference.png';Image.new('RGB',(256,256),(80,90,100)).save(media)
    asset_rows=[
        ('actor','character_sheet',{'character_id':'C1'}),
        ('costume','costume_sheet',{'character_ref':'C1'}),
        ('other','character_sheet',{'character_id':'C2'}),
        ('other-costume','costume_sheet',{'character_ref':'C2'}),
        ('scene','scene_sheet',{}),
    ]
    with Session.begin() as db:
        db.add(Record(id='packed-project',kind='director',data={'status':'generating'}))
        for asset_id,kind,spec in asset_rows:
            task_id='task-'+asset_id
            db.add(Task(id=task_id,kind='image',status='completed',payload={
                'creative_id':'packed-project','asset_kind':kind,'title':asset_id},result={'asset_id':asset_id}))
            db.add(Record(id=asset_id,kind='asset',data={'name':asset_id,'status':'approved',
                'media':'/media/recovery-reference.png','asset_kind':kind,'asset_spec':spec,'source_task':task_id}))
    config={'style':'固定二维漫画画风和冷色光线','assets':{
        'actor':{'role':'character','name':'女主','notes':'固定人物身份参考'},
        'costume':{'role':'costume','name':'通勤服装','notes':'固定通勤服装参考','identity_asset_id':'actor'},
        'other':{'role':'character','name':'跟踪者','notes':'固定人物身份参考'},
        'other-costume':{'role':'costume','name':'灰夹克','notes':'固定灰夹克服装参考','identity_asset_id':'other'},
        'scene':{'role':'scene','name':'餐厅','notes':'固定餐厅空间参考'},
    }}
    prep.save('packed-project',prep.Setup(**config));prep.approve_references('packed-project',prep.get('packed-project')['stamp'])
    with Session() as db:references=prep.ready(db,'packed-project')
    raw=board()
    for shot in raw['shots']:shot['assets']=['actor','costume','scene']
    raw['shots'][0]['assets']=['actor','costume','other','other-costume','scene']
    with Session.begin() as db:
        project=db.get(Record,'packed-project');project.data={**project.data,
            'task_id':'packed-child','board_diagnostics':{'raw':raw,'errors':[{'field':'assets','reason':'超限'}]}}
        run_items=[
            {'role':'character','character_id':'C1','name':'女主','task_id':'task-actor'},
            {'role':'costume','character_ref':'C1','name':'通勤服装','task_id':'task-costume'},
            {'role':'character','character_id':'C2','name':'跟踪者','task_id':'task-other'},
            {'role':'costume','character_ref':'C2','name':'灰夹克','task_id':'task-other-costume'},
            {'role':'scene','name':'餐厅','task_id':'task-scene'},
        ]
        db.add(Record(id='creative_packed-project',kind='creative_run',data={'stage':'storyboarding',
            'watch':'packed-watch','items':run_items,'direct_reference_inputs':True,'production_split':True}))
        db.add(Task(id='packed-child',kind='director',status='needs_review',payload={'mode':'live','stage':'board',
            'project_id':'packed-project','preproduction':references,'production_node':'packed-node',
            'source':{'content':TEXT}}))
        db.add(Task(id='packed-watch',kind='creative_watch',status='needs_review',payload={'mode':'live',
            'creative_id':'packed-project','child':'packed-child','production_node':'packed-node'}))
        db.add(Task(id='packed-node',kind='author_storyboard',status='needs_review',payload={'mode':'live',
            'work_id':'packed-work','run_id':'packed-run','phase':'storyboarding'}))
        db.add(Record(id='packed-work',kind='author_project',data={'director_id':'packed-project',
            'stage':'storyboarding','run_id':'packed-run','supervisor':'packed-node',
            'production_nodes':{'storyboarding':'packed-node'}}))
    response=client.post('/api/author/projects/packed-work/resume')
    assert response.status_code==200,response.text
    with Session() as db:
        project=db.get(Record,'packed-project');child=db.get(Task,'packed-child')
        current=prep.ready(db,'packed-project')
        packed=[(asset_id,spec) for asset_id,spec in current['assets'].items() if spec.get('costume_asset_id')]
        assert len(packed)==2 and all(db.get(Record,asset_id).data['derived_without_model'] for asset_id,_ in packed)
        assert child.status=='queued' and child.payload['preproduction']['stamp']==current['stamp']
        assert project.data['preproduction_stamp']==current['stamp']
        assert project.data['board_recovery']['conditional_stitching_added'] is True
        assert [change['action'] for change in project.data['board_recovery']['changes']]==[
            'pack_identity_costume_reference','pack_identity_costume_reference']
        assert all(db.get(Record,asset_id) is not None for asset_id,_,_ in asset_rows)
        from sqlalchemy import select
        assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==5
