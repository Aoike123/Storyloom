from backend import creative as c,preproduction as prep
from backend.db import Record,Session,Task
from test_director import recoverable_board,reference_prep


def test_author_resume_requeues_saved_storyboard_without_resubmitting_it(client,monkeypatch):
    raw=recoverable_board();references=reference_prep()
    monkeypatch.setattr(prep,'ready',lambda db,pid:references)
    with Session.begin() as db:
        db.add(Record(id='story-project',kind='director',data={
            'task_id':'story-child','board_diagnostics':{'raw':raw,'errors':[{'field':'assets','reason':'S02 duplicate'}]}}))
        db.add(Record(id='creative_story-project',kind='creative_run',data={
            'stage':'storyboarding','watch':'story-watch'}))
        db.add(Task(id='story-child',kind='director',status='needs_review',payload={
            'mode':'live','stage':'board','project_id':'story-project','preproduction':references,
            'production_phase':'storyboarding','production_node':'old-story-node'}))
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
        assert child.payload['saved_board']==raw
        assert child.payload['production_node']==watch.payload['production_node']==replacement.id
        assert replacement.status=='queued' and replacement.payload['revision_of']=='old-story-node'
        assert db.get(Task,'old-story-node').status=='superseded'
        assert db.get(Record,'story-project').data['board_recovery']['changes']
