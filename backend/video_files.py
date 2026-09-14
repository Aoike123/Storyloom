"""Probe and prepare independent video files using the bundled FFmpeg runtime."""
import hashlib
import os
import re
import shutil
import struct
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path

import imageio_ffmpeg

from .db import DATA


class StorageError(ValueError):
    pass


def media_path(url):
    root = (DATA / 'media').resolve()
    if not isinstance(url, str) or not url.startswith('/media/'):
        raise StorageError('视频或参考图必须是已保存的本地素材。')
    path = (root / url[len('/media/'):]).resolve()
    if not path.is_relative_to(root) or path == root or not path.is_file():
        raise StorageError('素材文件不存在或路径无效。')
    return path


def media_url(path):
    return '/media/' + Path(path).resolve().relative_to((DATA / 'media').resolve()).as_posix()


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def ffmpeg(args, timeout=180):
    try:
        return subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-nostdin', *map(str, args)],
            capture_output=True, check=True, timeout=timeout, encoding='utf-8', errors='replace',
        )
    except (subprocess.SubprocessError, OSError):
        raise StorageError('媒体检查或处理失败，请检查视频文件。') from None


def mp4_faststart(path):
    """Inspect top-level boxes, without reading the encoded payload into memory."""
    length = Path(path).stat().st_size
    with Path(path).open('rb') as stream:
        while stream.tell() + 8 <= length:
            start = stream.tell()
            size, kind = struct.unpack('>I4s', stream.read(8))
            if size == 1:
                extra = stream.read(8)
                if len(extra) != 8:
                    return False
                size = struct.unpack('>Q', extra)[0]
            if kind == b'moov':
                return True
            if kind in (b'mdat', b'moof') or size < 8 or start + size > length:
                return False
            stream.seek(start + size)
    return False


def probe_video(path):
    # framemd5 supplies decoded frame timestamps, count and rational timebase;
    # no ffprobe install and no duration*fps estimate are required.
    result = ffmpeg(['-v', 'info', '-xerror', '-copyts', '-i', path, '-map', '0:v:0', '-an',
                     '-fps_mode', 'passthrough', '-f', 'framemd5', '-'])
    tb = re.search(r'^#tb 0: (\d+)/(\d+)', result.stdout, re.M)
    dims = re.search(r'^#dimensions 0: (\d+)x(\d+)', result.stdout, re.M)
    frames = [line.split(',') for line in result.stdout.splitlines() if line and not line.startswith('#')]
    if not tb or not dims or not frames:
        raise StorageError('视频没有有效的画面和时间信息。')
    base = Fraction(int(tb[1]), int(tb[2]))
    pts = [int(f[2]) for f in frames]
    durations = [int(f[3]) for f in frames]
    step = durations[0]
    if step <= 0 or any(b <= a for a, b in zip(pts, pts[1:])):
        raise StorageError('视频帧时间无效。')
    constant = all(d == step for d in durations) and all(b - a == step for a, b in zip(pts, pts[1:]))
    fps = 1 / (base * step) if constant else None
    header = result.stderr.split('Stream mapping:')[0]
    video = re.search(r'Video: ([^\s,]+).*?,\s*(\w+)(?:\([^\n]*?\))?[, ]', header)
    audio = re.search(r'Audio: ([^\s,]+).*?,\s*(\d+) Hz,\s*([^,\n]+)', header)
    audio_profile = re.search(r'Audio: aac \(([^)]+)\)', header)
    container = re.search(r'Input #0, ([^\n]+?), from ', header)
    if not video or not container:
        raise StorageError('无法识别视频编码或容器。')
    family = container[1]
    if 'mp4' in family:
        with Path(path).open('rb') as stream:
            first = stream.read(16)
        fmt = 'mov' if first[8:12] == b'qt  ' else 'mp4'
    elif 'matroska' in family or 'webm' in family:
        fmt = 'mkv'
    elif family == 'avi':
        fmt = 'avi'
    else:
        raise StorageError('当前素材存储支持 MP4、MOV、Matroska 和 AVI 视频。')
    return {
        'container': fmt, 'video_codec': video[1], 'pixel_format': video[2],
        'width': int(dims[1]), 'height': int(dims[2]), 'frame_count': len(frames),
        'fps': {'num': fps.numerator, 'den': fps.denominator} if fps else None,
        'constant_frame_rate': constant,
        'time_base': {'num': base.numerator, 'den': base.denominator},
        'first_pts': pts[0], 'duration': float((pts[-1] + durations[-1] - pts[0]) * base),
        'audio': {'present': bool(audio), 'codec': audio[1] if audio else None,
                  'profile': audio_profile[1] if audio_profile else None,
                  'sample_rate': int(audio[2]) if audio else None,
                  'channels': {'mono': 1, 'stereo': 2}.get(audio[3]) if audio else None},
    }


def publish_file(temp, destination):
    """Atomically expose a complete same-volume file, without overwriting an immutable name."""
    try:
        os.link(temp, destination)
    except FileExistsError:
        if digest(temp) != digest(destination):
            raise StorageError('不可变文件位置已存在不同内容。') from None


def atomic_copy(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(source) != digest(destination):
            raise StorageError('不可变素材位置已存在不同的文件。')
        return destination
    temp = destination.with_name('.' + uuid.uuid4().hex + '.part')
    try:
        shutil.copyfile(source, temp)
        publish_file(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def prepare_playback(source, source_meta, folder):
    audio = source_meta['audio']
    compatible = (source_meta['video_codec'] == 'h264' and source_meta['pixel_format'] == 'yuv420p'
                  and source_meta['fps'] == {'num': 24, 'den': 1} and source_meta['first_pts'] == 0
                  and not source_meta['width'] % 2 and not source_meta['height'] % 2
                  and (not audio['present'] or (audio['codec'] == 'aac' and audio['profile'] == 'LC'
                                               and audio['sample_rate'] == 48000 and audio['channels'] == 2)))
    if compatible and source_meta['container'] == 'mp4' and mp4_faststart(source):
        return source, source_meta, 'reused'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'playback_v1.mp4'
    # An earlier attempt may have finished the file before the database commit.
    receipt = folder / '.playback_source.sha256'
    source_hash = digest(source)
    if destination.exists() and receipt.exists() and receipt.read_text() == source_hash:
        return destination, probe_video(destination), 'recovered'
    if destination.exists():
        raise StorageError('播放版本缺少可信的来源记录，请检查已有素材。')
    temp = folder / ('.' + uuid.uuid4().hex + '.mp4')
    try:
        args = ['-v', 'error', '-xerror', '-y', '-i', source, '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1']
        if compatible:
            args += ['-c', 'copy']
        else:
            args += ['-vf', 'fps=24:start_time=0,pad=ceil(iw/2)*2:ceil(ih/2)*2',
                     '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                     '-c:a', 'aac', '-profile:a', 'aac_low', '-ar', '48000', '-ac', '2']
        ffmpeg([*args, '-movflags', '+faststart', temp])
        meta = probe_video(temp)
        if meta['fps'] != {'num': 24, 'den': 1} or meta['first_pts'] != 0:
            raise StorageError('播放视频尚未满足帧时间规范。')
        # Receipt first: a crash can leave a harmless receipt, never an
        # untraceable published playback file.
        receipt.write_text(source_hash, encoding='ascii')
        publish_file(temp, destination)
        return destination, meta, 'remuxed' if compatible else 'transcoded'
    finally:
        temp.unlink(missing_ok=True)


def extract_boundary(source, index, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    temp = destination.with_name('.' + uuid.uuid4().hex + '.png')
    try:
        ffmpeg(['-v', 'error', '-xerror', '-y', '-i', source, '-vf', f'select=eq(n\\,{index})',
                '-frames:v', '1', '-fps_mode', 'passthrough', temp])
        if not temp.is_file():
            raise StorageError('无法提取实际剪辑边界帧。')
        publish_file(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
