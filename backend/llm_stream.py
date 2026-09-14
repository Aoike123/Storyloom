"""Consume one Chat Completions request without replaying a paid submission."""
import json

from .providers import ProviderError


def read_completion(response, on_event):
    if 'text/event-stream' not in response.headers.get('content-type', '').lower():
        # Some compatible gateways return JSON despite stream=true. Use that one response.
        response.read()
        data = response.json()
        on_event('buffered', '')
        return data

    content = ''
    usage = {}
    finish_reason = None
    signalled_reasoning = False
    lines = []

    def events():
        for line in response.iter_lines():
            if line == '':
                if lines:
                    yield '\n'.join(lines)
                    lines.clear()
            elif line.startswith('data:'):
                lines.append(line[5:].lstrip(' '))
        if lines:
            yield '\n'.join(lines)

    for event in events():
        if event.strip() == '[DONE]':
            break
        data = json.loads(event)
        if data.get('error'):
            raise ProviderError('模型输出途中返回错误，已保留构思草稿；本次请求不会自动重发。')
        if isinstance(data.get('usage'), dict):
            usage = data['usage']
        for choice in data.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if (delta.get('reasoning_content') or delta.get('reasoning')) and not signalled_reasoning:
                # Display a real activity signal, not raw internal reasoning or invented prose.
                signalled_reasoning = True
                on_event('reasoning', '')
            if isinstance(delta.get('content'), str) and delta['content']:
                content += delta['content']
                if len(content) > 262144:
                    raise ProviderError('模型输出超过本次可接收范围，已停止读取；不会自动重发。')
                on_event('delta', content)
            if choice.get('finish_reason') is not None:
                finish_reason = choice['finish_reason']

    if finish_reason is None:
        raise ProviderError('模型输出连接提前结束，已保留构思草稿；请核实本次调用后再重试。')
    return {'choices': [{'finish_reason': finish_reason, 'message': {'content': content}}], 'usage': usage}
