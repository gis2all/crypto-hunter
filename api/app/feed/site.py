"""站点身份的单一出处（AIHOT 的 `industry/site.ts`）。

站名、行业词、联系方式、采集 UA、对外基址 —— 这五样从前散在
`forms.py`、`http.py` 与各处文案里，于是“换个名字要改六个文件”。一处定义之后，
读者看到的每一个站名都是同一个。

`base_url()` 从环境读（`CH_SITE_URL`）—— 对外链接不能写死一个
本机地址：一份被转发出去的 RSS 里的链接要能打开。
"""

from __future__ import annotations

import os

#: 站名。页面与文档都读它，不各自写一遍。
NAME = "Crypto Hunter"
#: 中文站名。中文页面用它，与 `NAME` 一起出现在标题里。
NAME_ZH = "加密猎手"
#: 行业词。预筛与站点描述用它，而不是每处各写一个“加密”。
INDUSTRY = "加密行业"
#: 站点一句话。`about` 页与 `llms.txt` 都读它。
TAGLINE = "一个只看事实的加密行业信息流。"
#: 联系方式。反馈入口与静态页读它。
CONTACT_EMAIL = os.environ.get("CH_CONTACT_EMAIL", "hello@example.com")

#: 采集器自报的名字（AIHOT 的 `SITE.crawlerName`）。诚实声明自己是谁，
#: 不冒充浏览器 —— 对方的 robots 与封禁规则按 UA 放行或封禁。
CRAWLER_NAME = "CryptoHunterBot"
CRAWLER_HOMEPAGE = "https://github.com/crypto-hunter"


def base_url() -> str:
    """对外基址，无尾部斜杠。"""
    return (os.environ.get("CH_SITE_URL") or "").strip().rstrip("/")


def absolute(path: str) -> str:
    """相对路径 → 绝对地址。已经是绝对的原样还回。

    没配 `CH_SITE_URL` 时只能还回相对路径 —— 一个瞑的基址会把每一个链接都改成
    错的绝对地址，而相对链接至少在本机能用。
    """
    if not path:
        return path
    if path.startswith(("http://", "https://", "//", "mailto:")):
        return path
    base = base_url()
    if not base:
        return path
    return "%s/%s" % (base, path.lstrip("/"))


def crawler_headers() -> dict:
    """采集车道的 UA 与联系方式；与 AIHOT 同一形状。"""
    return {
        "User-Agent": "%s/1.0 (+%s; %s)" % (CRAWLER_NAME, CRAWLER_HOMEPAGE, CONTACT_EMAIL),
        "From": CONTACT_EMAIL,
    }


def with_subject(title: str) -> str:
    """页面标题后面跟上站名（AIHOT 的 `withSubject`）。"""
    clean = (title or "").strip()
    return "%s | %s" % (clean, NAME) if clean else NAME
