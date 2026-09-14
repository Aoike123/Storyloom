"""Versioned clip artifacts and immutable selections; DB records are authoritative."""
import copy
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .db import DATA, Record, Session, Task, record_dict
from .video_files import (StorageError, atomic_copy, digest, extract_boundary,
                          media_path, media_url, prepare_playback, probe_video)

router = APIRouter(prefix='/api', tags=['video-storage'])


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def identity(prefix, data):
    return prefix + '_' + hashlib.sha256(canonical(data).encode()).hexdigest()[:40]


def file_info(path, mime):
    checksum = digest(path)
    return {'file_id': 'file_' + checksum, 'storage_key': Path(path).resolve().relative_to(DATA).as_posix(),
            'media': media_url(path), 'mime': mime, 'byte_size': Path(path).stat().st_size, 'sha256': checksum}


def verify_file(info):
    path = media_path(info['media'])
    if path.stat().st_size != info['byte_size'] or digest(path) != info['sha256']:
        raise StorageError('素材文件已变化，与保存的版本摘要不一致。')
    return path


def immutable(db, rid, kind, data):
    existing = db.get(Record, rid)
    if existing:
        if existing.kind != kind or existing.data != data:
            raise StorageError('不可变版本记录已存在不同内容。')
        return existing
    row = Record(id=rid, kind=kind, data=copy.deepcopy(data))
    db.add(row)
    db.flush()
    return row


def snapshot_asset(db, asset, version=None):
    if version is not None and asset.version != version:
        raise StorageError('参考素材版本已变化，请重新提交。')
    rid = identity('assetrev', [asset.id, asset.version])
    data = {'schema_version': 1, 'asset_id': asset.id, 'revision': asset.version,
            'name': asset.data.get('name'), 'type': asset.data.get('type'),
            'description': asset.data.get('description'), 'file': None}
    if asset.data.get('media'):
        path = media_path(asset.data['media'])
        from PIL import Image
        with Image.open(path) as im:
            fmt = im.format
            im.verify()
        mime, suffix = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'),
                        'WEBP': ('image/webp', '.webp')}.get(fmt, (None, None))
        if not mime:
            raise StorageError('当前参考素材快照支持 PNG、JPEG 和 WebP。')
        frozen = DATA / 'media' / 'assets' / 'snapshots' / (digest(path) + suffix)
        atomic_copy(path, frozen)
        data['file'] = file_info(frozen, mime)
    return immutable(db, rid, 'asset_revision', data)


def generation_snapshot(db, payload, director=None, shot=None, frame_asset=None,*,reference_mode=False):
    """Freeze actual input assets before the task is dispatched, not after generation."""
    bindings = []
    if frame_asset:
        frame = snapshot_asset(db, frame_asset, payload.get('asset_version'))
        bindings.append({'role': 'shot_reference' if reference_mode else 'first_frame', 'asset_revision_id': frame.id, **frame.data})
    refs = []
    if director and shot:
        from .consistency import config
        cfg = config(db, director.id)
        if cfg:
            refs = [{'id': aid, 'version': cfg.data.get('asset_versions', {}).get(aid)}
                    for aid in cfg.data.get('bindings', {}).get(shot['id'], [])]
    if not refs and frame_asset:
        refs = frame_asset.data.get('reference_assets') or []
    for ref in refs:
        asset = db.get(Record, ref['id'])
        if not asset or asset.kind != 'asset':
            raise StorageError('分镜绑定的参考素材不存在。')
        frozen = snapshot_asset(db, asset, ref.get('version'))
        if not any(binding['asset_id']==asset.id for binding in bindings):
            bindings.append({'role': 'reference', 'asset_revision_id': frozen.id, **frozen.data})
    shot_ref = None
    if director and shot:
        data = {'schema_version': 1, 'story_id': director.data.get('source_id'),
                'director_id': director.id, 'shot_id': shot['id'], 'revision': director.version,
                'spec': copy.deepcopy(shot), 'asset_bindings': bindings}
        shot_row = immutable(db, identity('shotrev', [director.id, director.version, shot['id'],
                                                    payload.get('consistency_stamp')]+(['reference-images-v1'] if reference_mode else [])), 'shot_revision', data)
        shot_ref = {'id': shot_row.id, 'story_id': data['story_id'], 'director_id': director.id,
                    'shot_id': shot['id'], 'revision': director.version}
    external = ({'url_sha256': hashlib.sha256(payload['image_url'].encode()).hexdigest(), 'archived': False}
                if payload.get('image_url') else None)
    return {'schema_version': 1, 'snapshot_status': 'partial_external_input' if external else 'captured', 'shot': shot_ref,
            'asset_bindings': bindings, 'prompt': payload.get('prompt', ''),
            **({'input_mode':'reference_images','external_reference':external} if reference_mode else {'external_first_frame':external}),
            'requested_generation_seconds': payload.get('generation_seconds'),
            'requested_edit_seconds': payload.get('edit_seconds')}


def register_artifact(db, clip_id, source, provenance=None, keep_source=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', clip_id):
        raise StorageError('片段标识无效。')
    rid = identity('artifact', clip_id)
    existing = db.get(Record, rid)
    if existing:
        verify_file(existing.data['source'])
        verify_file(existing.data['playback'])
        if digest(source) != existing.data['source']['sha256']:
            raise StorageError('同一片段不能替换为另一份生成结果。')
        return existing
    meta = probe_video(source)
    folder = DATA / 'media' / 'clips' / clip_id
    stored = Path(source) if keep_source else atomic_copy(source, folder / ('source.' + meta['container']))
    playable, playback_meta, preparation = prepare_playback(stored, meta, folder)
    mime = {'mp4': 'video/mp4', 'mov': 'video/quicktime', 'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo'}
    data = {'schema_version': 1, 'clip_id': clip_id,
            'source': {**file_info(stored, mime[meta['container']]), 'properties': meta},
            'playback': {**file_info(playable, 'video/mp4'), 'properties': playback_meta},
            'preparation': preparation, 'provenance': copy.deepcopy(provenance or {}),
            'quality_status_at_ingest': 'unreviewed'}
    return immutable(db, rid, 'clip_artifact', data)


def attach_artifact(clip, artifact):
    # Legacy releases remain readable with their original file and second offsets.
    updates = {'artifact_id': artifact.id, 'storage_schema_version': 1}
    if not clip.data.get('locked'):
        updates.update(media=artifact.data['playback']['media'],
                       duration=artifact.data['playback']['properties']['duration'])
    clip.data = {**clip.data, **updates}


def ensure_artifact(db, clip):
    if clip.data.get('artifact_id'):
        row = db.get(Record, clip.data['artifact_id'])
        if not row or row.kind != 'clip_artifact' or row.data['clip_id'] != clip.id:
            raise StorageError('片段的存储记录不完整。')
        verify_file(row.data['source'])
        verify_file(row.data['playback'])
        return row
    task = db.get(Task, clip.data.get('source_task', ''))
    provenance = {'snapshot_status': 'legacy_unverified', 'source_task': clip.data.get('source_task')}
    if task:
        provenance.update(task_id=task.id, provider_id=task.result.get('provider_id'),
                          input_snapshot=task.payload.get('input_snapshot'))
    row = register_artifact(db, clip.id, media_path(clip.data['media']), provenance, keep_source=True)
    attach_artifact(clip, row)
    return row


def annotation_snapshot(db, clip, artifact):
    if not clip.data.get('annotated'):
        return None
    data = {'schema_version': 1, 'clip_id': clip.id, 'clip_record_version': clip.version,
            'playback_file_id': artifact.data['playback']['file_id'],
            'events_seconds': copy.deepcopy(clip.data.get('events', [])),
            # Old manually authored seconds have not been checked against a new rendition.
            'timing_status': 'requires_frame_review'}
    return immutable(db, identity('annotation', data), 'clip_annotation', data).id


def create_use(db, clip, start, end, shot):
    artifact = ensure_artifact(db, clip)
    playback = artifact.data['playback']
    meta = playback['properties']
    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= meta['duration'] + 1e-6:
        raise StorageError('选取区间超出实际视频时长。')
    fps = meta['fps']['num'] / meta['fps']['den']
    first = max(0, math.ceil(start * fps - 1e-8))
    stop = min(meta['frame_count'], math.ceil(end * fps - 1e-8))
    if first >= stop:
        raise StorageError('选取区间未包含完整可显示帧。')
    annotation_id = annotation_snapshot(db, clip, artifact)
    core = {'schema_version': 1, 'clip_id': clip.id, 'artifact_id': artifact.id,
            'playback_file_id': playback['file_id'], 'shot': shot,
            'range': {'in_frame': first, 'out_frame': stop},
            'annotation_revision_id': annotation_id}
    rid = identity('use', core)
    existing = db.get(Record, rid)
    if existing:
        verify_use(db, existing)
        return existing
    folder = DATA / 'media' / 'clip_uses' / rid
    boundaries = {}
    for label, index in (('first', first), ('last', stop - 1)):
        dest = folder / ('in_frame.png' if label == 'first' else 'out_frame.png')
        extract_boundary(verify_file(playback), index, dest)
        boundaries[label] = {'frame_index': index, **file_info(dest, 'image/png')}
    data = {**core, 'requested_seconds': {'start': start, 'end': end},
            'start': first / fps, 'end': stop / fps, 'boundaries': boundaries,
            'visual_reviewed': True}
    return immutable(db, rid, 'clip_use', data)


def verify_use(db, use):
    artifact = db.get(Record, use.data['artifact_id'])
    if not artifact or artifact.kind != 'clip_artifact' or artifact.data['clip_id'] != use.data['clip_id']:
        raise StorageError('选用记录引用的片段不存在。')
    playback = artifact.data['playback']
    data = use.data
    bounds = data['range']
    if data['playback_file_id'] != playback['file_id'] or not 0 <= bounds['in_frame'] < bounds['out_frame'] <= playback['properties']['frame_count']:
        raise StorageError('选用记录与视频帧范围不匹配。')
    verify_file(playback)
    for name, index in (('first', bounds['in_frame']), ('last', bounds['out_frame'] - 1)):
        if data['boundaries'][name]['frame_index'] != index:
            raise StorageError('剪辑边界帧不匹配。')
        verify_file(data['boundaries'][name])
    return artifact


def use_entry(db, use, occurrence_id):
    artifact = verify_use(db, use)
    return {'occurrence_id': occurrence_id, 'status': 'ready', 'use_id': use.id,
            'artifact_id': artifact.id, 'clip_id': use.data['clip_id'],
            'playback_file_id': use.data['playback_file_id'], 'media': artifact.data['playback']['media'],
            'start': use.data['start'], 'end': use.data['end'],
            'shot_id': use.data['shot'].get('shot_id'), 'shot_revision': use.data['shot'].get('revision'),
            'range': use.data['range'], 'boundaries': use.data['boundaries']}


def export_manifest(release):
    """Derived export; a retry repairs a missing export from the committed DB record."""
    data = {'schema_version': release.data.get('schema_version', 0), 'release_id': release.id,
            'revision': release.version, **release.data}
    folder = DATA / 'manifests' / 'releases' / release.id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'v{release.version}.json'
    encoded = canonical(data)
    if path.exists():
        if path.read_text(encoding='utf-8') != encoded:
            raise StorageError('已发布的清单快照存在不同内容。')
        return path
    temp = folder / ('.' + uuid.uuid4().hex + '.json')
    try:
        temp.write_text(encoded, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def storage_view(db, clip):
    artifact = db.get(Record, clip.data.get('artifact_id', ''))
    use = db.get(Record, clip.data.get('selected_use_id', ''))
    return {'clip_id': clip.id, 'schema_version': 1 if artifact else 0,
            'artifact': record_dict(artifact) if artifact else None,
            'selected_use': record_dict(use) if use else None}


@router.get('/clips/{cid}/storage')
def read_storage(cid: str):
    with Session() as db:
        clip = db.get(Record, cid)
        if not clip or clip.kind != 'clip':
            raise HTTPException(404, '视频片段不存在。')
        return storage_view(db, clip)


@router.post('/clips/{cid}/register-storage')
def register_legacy(cid: str):
    try:
        with Session.begin() as db:
            clip = db.get(Record, cid)
            if not clip or clip.kind != 'clip':
                raise HTTPException(404, '视频片段不存在。')
            ensure_artifact(db, clip)
            return storage_view(db, clip)
    except StorageError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('/reader/releases/{rid}/manifest')
def read_manifest(rid: str):
    with Session() as db:
        row = db.get(Record, rid)
        if not row or row.kind != 'reader_release':
            raise HTTPException(404, '发布版本不存在。')
        return {'schema_version': row.data.get('schema_version', 0), 'release_id': row.id,
                'revision': row.version, **row.data}
