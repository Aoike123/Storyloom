"""Bounded, read-only previews of an unfinished style recommendation.

Drafts are display data only. The complete response still passes Options validation.
"""
import json


def partial_json(text):
    text = text[:262144]
    size = len(text)

    def whitespace(index):
        while index < size and text[index].isspace():
            index += 1
        return index

    def string(index):
        start = index
        index += 1
        while index < size:
            if text[index] == '\\':
                index += 2
            elif text[index] == '"':
                try:
                    return json.loads(text[start:index + 1]), index + 1
                except ValueError:
                    return None, size
            else:
                index += 1
        # A chunk may end inside an escape sequence. Keep only the decoded prefix.
        for trim in range(7):
            candidate = text[start:size - trim] if trim else text[start:]
            try:
                return json.loads(candidate + '"'), size
            except ValueError:
                pass
        return None, size

    def value(index, depth=0):
        index = whitespace(index)
        if index >= size or depth > 8:
            return None, size
        char = text[index]
        if char == '"':
            return string(index)
        if char == '{':
            result = {}
            index = whitespace(index + 1)
            while index < size and text[index] == '"':
                key, index = string(index)
                index = whitespace(index)
                if not isinstance(key, str) or index >= size or text[index] != ':':
                    break
                item, index = value(index + 1, depth + 1)
                if item is not None:
                    result[key] = item
                index = whitespace(index)
                if index >= size or text[index] != ',':
                    break
                index = whitespace(index + 1)
            return result, index + 1 if index < size and text[index] == '}' else index
        if char == '[':
            result = []
            index = whitespace(index + 1)
            while index < size and text[index] != ']':
                item, next_index = value(index, depth + 1)
                if item is not None:
                    result.append(item)
                if next_index <= index:
                    break
                index = whitespace(next_index)
                if index >= size or text[index] != ',':
                    break
                index = whitespace(index + 1)
            return result, index + 1 if index < size and text[index] == ']' else index
        try:
            item, end = json.JSONDecoder().raw_decode(text, index)
            return item, end
        except ValueError:
            return None, size

    return value(0)[0]


def style_preview(text):
    parsed = partial_json(text)
    if not isinstance(parsed, dict):
        return {'summary': '', 'options': []}
    summary = parsed.get('approach')
    options = parsed.get('options')
    def display(value,limit):
        # A transport chunk can split a JSON surrogate pair. Never persist a lone surrogate.
        return ''.join(char for char in value[:limit] if not 0xD800<=ord(char)<=0xDFFF)
    return {
        'summary': display(summary,1200) if isinstance(summary, str) else '',
        'options': [
            {key: display(item[key],limit) for key, limit in [('art', 500), ('tone', 300), ('reason', 500), ('prompt',500)] if isinstance(item.get(key), str)}
            for item in options[:5] if isinstance(item, dict)
        ] if isinstance(options, list) else [],
    }
