"""Story source content validation — the project-scoped original-text helper.

The story market and the local import flow were removed (D09 P3b): original text now lives in each
project as its own private ``story_source`` record, imported by the workbench (see ``authors.py``).
This module keeps only the content-validation helper that project story import needs.
"""

from fastapi import HTTPException

MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2MB of source text


def _check_content(content):
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(422, "正文不能为空。")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(422, "正文超过 2MB 限制，请缩短后再导入。")
