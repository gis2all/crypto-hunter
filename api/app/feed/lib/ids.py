"""稳定哈希与 id 原语（AIHOT 的 `lib/ids.ts`）。

三件事, 每件只有一个写法:

1. `stable_json` —— 字典转成字节上确定的一串（键排序、无空白）。
   同一份数据在两个进程里算出同一串，才能当幂等键用。
2. `sha256_hex` / `short_hash` —— 内容身份。与 `materials.content_hash`
   不同：那一个是“正文身份”的专用口径，这一个是通用原语。
3. `new_id` —— 时间排序的短 id（cuid2 的等价物：前缀可排序、
   后缀随机）。用在需要“不碰车且能春天排序”的地方。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def stable_json(value) -> str:
    """字典转字符串: 键排序、无空白、不转义非 ASCII。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return sha256_hex(text)[:length]


def hash_of(value) -> str:
    """任意可序列化的东西的内容身份。"""
    return sha256_hex(stable_json(value))


def idempotency_key(*parts) -> str:
    """逻辑请求的幂等键：决定输出的那一切的哈希。"""
    return hash_of([p if isinstance(p, str) else stable_json(p) for p in parts])


def _base36(number: int) -> str:
    if number <= 0:
        return "0"
    out = []
    while number:
        number, rem = divmod(number, 36)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_id(prefix: str = "", length: int = 10) -> str:
    """前缀可排序（毫秒时间的 36 进制）+ 随机后缀。"""
    stamp = _base36(int(time.time() * 1000))
    noise = _base36(int.from_bytes(os.urandom(8), "big"))
    body = (stamp + noise)[: max(6, length)]
    return "%s%s" % (prefix, body)


def b64url_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> str:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8")
