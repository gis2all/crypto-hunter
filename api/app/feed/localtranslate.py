'''本地翻译: Argos, 离线, CPU.

正文那一档原本整个是模型翻的 —— 一段一段喂进去, 一段一段中文换回来. 这一层不做那件事: 它只把
**纯文字**换一个语言, 块与小标题留在原地, 由 `htmlbody.rewrite_html` 拼回去 (`writing.py` 的
翻译档按 `pipeline.stages()['translate']` 分派). 代价写在明处: 块里行内的链接与代码换不回来,
那一块译出来的是它的纯文字 —— 比一句英文都没有好, 比模型译的那一份差.

装的是什么: `argostranslate` 与 en->zh 语言包. 包在第一次用到时才下载 (~100MB), 之后离线.
Argos 自己依赖 stanza, 而 stanza 依赖 torch —— 所以这一次的依赖比看上去重 (450MB, 见
`pyproject.toml` 那一段). 换一个更轻的引擎是另一件事, 不在这里顺手做.

不可用是常态, 不是故障: 没装、语言包没下、这一条本来就是中文、Argos 自己抛错 —— 四种都回
`None`, 调用方拿原文顶上. 与模型翻译失败时走的是同一条路, 所以页面不会因为少一个包而空掉.
'''

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

log = logging.getLogger(__name__)

#: 源语言 -> 目标语言. 这一版只做英文到中文: 中文的材料本来就不用译, 而其他语种的源还没收进来.
SOURCE = 'en'
TARGET = 'zh'

#: 引擎只找一次. 三态: `None` = 还没找过, `False` = 找过了没有, 别的 = 那个翻译对象.
#: 用锁是因为写作站跑在线程池里 (writing.py 的 concurrency), 第一次装载会撞上.
_engine: object = None
_lock = threading.Lock()


def reset() -> None:
    '''把「找过了」这件事忘掉. 装好语言包不用重启进程 (测试与自检都用它).'''
    global _engine
    with _lock:
        _engine = None


def _lookup() -> object:
    '''现找一次引擎. 找不到记 `False`, 免得每条材料都 import 一次再失败一次.'''
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        try:
            from argostranslate import translate as argos
            found = argos.get_translation_from_codes(SOURCE, TARGET)
        except Exception as exc:  # 没装、没下语言包、包坏了: 都当「这条路今天不通」
            log.info('localtranslate: argos unavailable (%s)', str(exc)[:160])
            found = None
        _engine = found if found else False
        return _engine


def available() -> bool:
    '''这一台机器现在能不能本地翻译. 预设分派不看它 —— 这里只是自检.'''
    return bool(_lookup())


def translate(text: str) -> str | None:
    '''一段纯文字的中文. 译不动回 `None`, 一句话都不抛 —— 调用方按「这一块没译上」处理.'''
    source = (text or '').strip()
    if not source:
        return None
    engine = _lookup()
    if not engine:
        return None
    try:
        answer = engine.translate(source)
    except Exception as exc:
        log.warning('localtranslate: translate failed (%s)', str(exc)[:160])
        return None
    answer = (answer or '').strip()
    return answer or None


def translate_all(texts: Sequence[str]) -> list[str | None]:
    '''一段一块, 顺序与条数都不变: 译不上的是 `None`, 与 `writing.translate_blocks` 同一个约定.'''
    return [translate(text) for text in texts]
