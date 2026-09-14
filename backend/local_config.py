import os
import tempfile
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from dotenv import set_key
from .environment import DEFAULTS, env_file

lock = Lock()
FIELDS = set(DEFAULTS)|{'LLM_PROVIDER','LLM_ENDPOINT'}


def save_config(values):
    if set(values) - FIELDS:
        raise ValueError('配置包含不支持的字段。')
    for key, value in values.items():
        if not isinstance(value, str) or len(value)>4096 or any(c in value for c in '\r\n\x00'):
            raise ValueError('配置格式无效，请检查输入。')
        if (key.endswith('_ENDPOINT') or key=='LLM_BASE_URL') and value:
            parsed=urlparse(value)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('接口地址必须是 HTTPS 地址，且不能包含账号或密码。')
    if values.get('LLM_PROVIDER','deepseek') not in ('deepseek','compatible') or values.get('VIDEO_PROVIDER','minimax') not in ('minimax','ark'):
        raise ValueError('不支持该供应商。')
    if values.get('IMAGE_PROVIDER','siliconflow') not in ('','siliconflow'):
        raise ValueError('当前生图接口支持硅基流动协议。')
    if 'ALLOW_PAID_CALLS' in values and values['ALLOW_PAID_CALLS'] not in ('true','false'):
        raise ValueError('付费开关格式无效。')
    with lock:
        target=env_file()
        target.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.config-',suffix='.tmp',dir=target.parent)
        os.close(fd)
        try:
            temp=Path(name)
            temp.write_text(target.read_text(encoding='utf-8') if target.exists() else '',encoding='utf-8')
            for key,value in values.items():
                if key.endswith('_API_KEY') and not value: continue
                set_key(name,key,value,quote_mode='always')
            os.replace(name,target)
        finally:
            if os.path.exists(name): os.unlink(name)
