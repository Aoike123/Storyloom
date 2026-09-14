import math
import os
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg
from .db import DATA

def font(size):
    choices = ['C:/Windows/Fonts/msyh.ttc','C:/Windows/Fonts/simhei.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    return ImageFont.truetype(next(x for x in choices if Path(x).exists()), size)

def scene_image(title, scene='platform'):
    w,h=960,540
    im=Image.new('RGB',(w,h),'#111c2c'); d=ImageDraw.Draw(im)
    warm=scene=='security'
    for y in range(h):
        t=y/h
        d.line((0,y,w,y),fill=(int(17+14*t),int(28+15*t),int(44+17*t)))
    if warm:
        d.rectangle((580,60,900,325), fill='#344456',outline='#b99c6a',width=5)
        d.line((740,60,740,325),fill='#b99c6a',width=5)
        d.rectangle((140,350,860,386), fill='#a2825e')
        d.polygon([(630,70),(545,180),(715,180)],fill='#d5b57c')
    else:
        d.polygon([(0,0),(960,0),(760,110),(0,140)],fill='#293746')
        for x in (140,450,760):
            d.polygon([(x,100),(x+22,98),(x+22,410),(x,410)],fill='#52606a')
        d.line((0,440,960,365),fill='#c5aa76',width=3)
        d.line((0,485,960,405),fill='#6d8293',width=2)
        d.rectangle((260,310,540,330),fill='#897860')
        d.rectangle((275,330,287,410),fill='#4e5b64'); d.rectangle((515,330,527,390),fill='#4e5b64')
        d.rectangle((351,365,399,391),fill='#b49a73')
        for i in range(95):
            x=(i*137)%960;y=(i*89)%450
            d.line((x,y,x-15,y+35),fill='#40576b',width=1)
        d.rounded_rectangle((120,60,355,108),radius=3,fill='#152a37',outline='#9bb4b5')
        d.text((138,69),'旧城站  /  末班',font=font(22),fill='#cbd8d6')
    # Deliberately schematic silhouettes; no claim of AI-generated artwork.
    d.ellipse((598,198,642,244),fill='#e0c8ad')
    d.pieslice((590,185,649,250),180,360,fill='#172030')
    d.polygon([(594,242),(646,242),(674,387),(574,387)],fill='#c8bfac')
    d.line((605,386,599,452),fill='#172130',width=14);d.line((642,386,650,452),fill='#172130',width=14)
    if not warm:
        d.ellipse((806,204,849,251),fill='#202637');d.polygon([(800,240),(857,240),(875,393),(786,393)],fill='#172130')
        d.line((813,391,807,455),fill='#121c29',width=14);d.line((850,391,859,450),fill='#121c29',width=14)
    d.rectangle((0,470,960,540),fill='#101827')
    d.text((32,488),title[:28],font=font(24),fill='#e8e4dc')
    d.text((32,20),'叙间  /  程序示意动画 · 非 AI 成片',font=font(16),fill='#b9c7cf')
    return im

def render_demo(clip_id,title,duration,scene='platform'):
    dest=DATA/'media'/f'{clip_id}.mp4'
    if dest.exists(): return f'/media/{dest.name}'
    still=DATA/'media'/f'{clip_id}.png'
    scene_image(title,scene).save(still)
    tmp=dest.with_suffix('.part.mp4')
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    filt="scale=1024:576,zoompan=z='min(zoom+0.00015,1.07)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=960x540:fps=24"
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-loop','1','-i',str(still),
                    '-t',str(duration),'-vf',filt,'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
                    '-movflags','+faststart',str(tmp)],check=True,timeout=120,capture_output=True)
    os.replace(tmp,dest)
    return f'/media/{dest.name}'

def inspect_video(path):
    reader=imageio_ffmpeg.read_frames(str(path),pix_fmt='rgb24')
    try:
        meta=next(reader)
        if not meta.get('duration') or meta['duration']<=0: raise ValueError('视频没有有效时长')
        return {'duration':round(meta['duration'],3),'size':list(meta['size'])}
    finally:
        reader.close()

def export_entries(entries, clips, output_id):
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    segments=[]
    for i, entry in enumerate(entries):
        clip=clips[entry['clip_id']]
        from .video_files import media_path
        source=media_path(clip['media'])
        part=DATA/'media'/f'{output_id}_{i}.mp4'
        start=entry.get('start',0);end=entry.get('end',clip['duration'])
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(source),'-ss',str(start),'-t',str(end-start),
                        '-vf','scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,fps=24',
                        '-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(part)],check=True,capture_output=True,timeout=180)
        segments.append(part)
    listing=DATA/'media'/f'{output_id}.txt'
    listing.write_text('\n'.join(f"file '{p.name}'" for p in segments),encoding='utf-8')
    dest=DATA/'media'/f'{output_id}.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),
                    '-c','copy','-movflags','+faststart',str(dest)],check=True,capture_output=True,timeout=180)
    return f'/media/{dest.name}'

if __name__=='__main__':
    from .db import init_db
    from .seed import seed,CLIPS
    init_db();seed()
    for c in CLIPS:
        print(render_demo(c['id'],c['title'],c['duration'],c['scene']))
