import os
import time
import uuid
from pathlib import Path

from .environment import load_bootstrap_environment
from sqlalchemy import JSON, Float, Integer, String, create_engine, delete, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
load_bootstrap_environment()
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'media').mkdir(exist_ok=True)
URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA / "story.db"}')
engine = create_engine(URL, connect_args={'check_same_thread': False, 'timeout': 30} if URL.startswith('sqlite') else {}, pool_pre_ping=True)
if URL.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_setup(connection, _):
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA foreign_keys=ON')
Session = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Record(Base):
    __tablename__ = 'records'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[float] = mapped_column(Float, default=time.time)

class Task(Base):
    __tablename__ = 'tasks'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    session_id: Mapped[str] = mapped_column(String(80), default='')
    revision: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str] = mapped_column(String(1000), default='等待执行')
    progress: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[float] = mapped_column(Float, default=0)
    owner: Mapped[str] = mapped_column(String(80), default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[float] = mapped_column(Float, default=time.time)

def uid(prefix):
    return f'{prefix}_{uuid.uuid4().hex[:16]}'

def record_dict(row):
    return {'id': row.id, 'version': row.version, **row.data}


def save_generation_request(task_id,*,model,prompt,image_size=None,reference_count=0,input_mode=None,inference_steps=None):
    # Save the text actually submitted, without credentials or signed reference URLs.
    with Session.begin() as db:
        task=db.get(Task,task_id)
        if task:
            task.result={**task.result,'generation_request':{'model':model,'prompt':prompt,
                'image_size':image_size,'reference_count':reference_count,**({'input_mode':input_mode} if input_mode else {}),
                **({'inference_steps':inference_steps} if inference_steps is not None else {})}}


def generation_debug(task):
    if task.kind not in ('image','video'):return None
    metadata={k:task.payload.get(k) for k in ('asset_kind','asset_schema','character_key','costume_key','identity_asset_id','costume_asset_id','node_skill','prompt_source','revision_instruction','regeneration_mode')}
    if task.payload.get('edit_instruction'):metadata['edit_instruction']=task.payload['edit_instruction']
    if task.payload.get('rendering_style'):metadata['rendering_style']=task.payload['rendering_style']
    if task.payload.get('reference_roles'):metadata['reference_roles']=task.payload['reference_roles']
    style_reference=task.payload.get('style_reference') or {}
    if style_reference.get('file',{}).get('media'):
        metadata['style_reference_media']=(style_reference.get('texture_file') or style_reference['file'])['media']
    request=task.result.get('generation_request')
    if isinstance(request,dict) and isinstance(request.get('prompt'),str):
        return {**{k:request.get(k) for k in ('prompt','model','image_size','reference_count','inference_steps')},
                **({'input_mode':request['input_mode']} if request.get('input_mode') else {}),'source':'request',**metadata}
    prompt=task.payload.get('prompt')
    return {'prompt':prompt,'source':'task',**metadata} if isinstance(prompt,str) else None

def task_dict(row):
    from .task_activity import activity_data
    from .image_errors import task_error
    from .image_messages import completed_message
    from sqlalchemy.orm import object_session
    session=object_session(row)
    trace=session.get(Record,'skill_trace_'+row.id) if session else None
    references=row.payload.get('reference_media')
    preview=row.payload.get('frame_media') or (references[0] if isinstance(references,list) and references else None)
    preview=preview if isinstance(preview,str) and preview.startswith('/media/') else None
    return {'id': row.id, 'kind': row.kind, 'status': row.status, 'session_id': row.session_id,
            'production_phase':row.payload.get('production_phase') or row.payload.get('phase'),
            'revision': row.revision, 'progress': row.progress, 'message': completed_message(row.payload) if row.kind=='image' and row.status=='completed' else row.message,
            'result': row.result, 'created': row.created, 'attempts': row.attempts,
            'mode': row.payload.get('mode', 'demo'), 'label': row.payload.get('title') or row.payload.get('shot_id'),
            'generation':generation_debug(row),'provider_error':task_error(row),'skill_calls':trace.data['calls'] if trace else [],
            'activity': activity_data(row),'preview':preview,'revision_of':row.payload.get('revision_of')}

def init_db():
    Base.metadata.create_all(engine)
    # Retire old sign-in data while preserving stories, media, jobs, and attribution.
    with Session.begin() as db:
        db.execute(delete(Record).where(Record.kind.in_(['author_account', 'author_session'])))
        for row in db.scalars(select(Record).where(Record.kind.in_(['author_project', 'story_source', 'asset']))):
            field = 'scope' if row.kind == 'asset' else 'owner'
            if field in row.data:
                row.data = {k: v for k, v in row.data.items() if k != field}
    (DATA / 'admin-access.txt').unlink(missing_ok=True)
