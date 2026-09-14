"""Fetch pinned, text-only upstream skills used by the workflow node adapters."""
import hashlib
import json
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]/'backend'/'node_skills'
SOURCES={
    'film':{'repo':'zhangzhangco/film-production-skills','commit':'47b2a6a432235e716fa2aa0d08eefae76fdb34fd','license':'MIT',
        'skills':['structure-screenplay','shape-story-blueprint','design-production-assets','plan-camera-shots','compile-generation-prompts','review-and-assemble']},
    'replicate':{'repo':'replicate/skills','commit':'2f36e415965ae63baa1c9f6635888092bcd771d3','license':'Apache-2.0','skills':['prompt-images']},
}


def main():
    lock={'version':1,'sources':{}}
    with httpx.Client(timeout=30,follow_redirects=True) as client:
        for key,source in SOURCES.items():
            response=client.get(f'https://api.github.com/repos/{source["repo"]}/git/trees/{source["commit"]}?recursive=1')
            response.raise_for_status()
            files={}
            for entry in response.json()['tree']:
                path=entry['path']
                selected=path in ('LICENSE','NOTICE') or (key=='film' and path.startswith('schemas/common/'))
                selected=selected or any(path.startswith('skills/'+name+'/') for name in source['skills'])
                if entry['type']!='blob' or not selected or not (path in ('LICENSE','NOTICE') or path.endswith(('.md','.json'))):continue
                target=(ROOT/'vendor'/key/path).resolve()
                if not target.is_relative_to((ROOT/'vendor'/key).resolve()):raise ValueError('Unsafe upstream path')
                response=client.get(f'https://raw.githubusercontent.com/{source["repo"]}/{source["commit"]}/{path}')
                response.raise_for_status();content=response.content
                if target.exists() and target.read_bytes()!=content:raise ValueError('Refusing to overwrite modified vendor file: '+path)
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
                files[path]=hashlib.sha256(content).hexdigest()
            lock['sources'][key]={**source,'files':files}
            print(key,source['commit'],len(files),'text files',flush=True)
    (ROOT/'vendor-lock.json').write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
