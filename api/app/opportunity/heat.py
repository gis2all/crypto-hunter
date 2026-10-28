"""热度：一条材料自身带来的关注，而不是全站计数。

这个模块**没有**计数接口可用 —— 参考产品的热度来自 X 的逐小时帖子计数
（`posts_counts_recent`），而本项目的 X 通道是一个 CLI，没有这个能力。所以热度在这里是
另一件事，口径也必须写成另一件事：

- **热度＝来源帖自身的互动量**（浏览 / 赞 / 转发 / 评论），按一个写明的加权换算成 0-100。
- **没有来源帖就没有热度**：平台页、交易所公告、GitHub 仓库都只有页面材料，它们的 `heat`
  是 `None` —— 页面上显示「—」，不是 0。
- **它不做排序键**：排序键是本项目自己的 `discovered_at`（各来源答案一致的那个）。热度只
  是徽章与筛选项，因为它是一个来源自己报的数，换一批源就会变。

权重写在代码里而不是凭感觉调，因为徽章上的分档要能解释：一次转发的传播面大于一次点赞，
一条评论是一次互动，浏览是分母。
"""

from __future__ import annotations

import math

#: 每一项互动折成多少个「浏览当量」。写死，并且只在这一处。
WEIGHTS = {"views": 1, "likes": 30, "retweets": 60, "replies": 30}

#: 100 分对应的加权互动量。对数刻度：一万次浏览与十万次浏览之间的差别不是十倍热度，
#: 而线性刻度会让一条爆款把其他所有条目压成 0 分。
#:
#: 200 万这个数是实测定的：2026-10-18 那轮扫描里最热的一条（Pumpfun，141 万浏览 + 3,586 赞）
#: 加权约 158 万，落在刻度顶端附近。刻度改了就重算（`extract.refresh_heat`），所以它不是
#: 一个写完就固定的魔法数，而是一个可以按新数据调整的口径。
FULL_SCALE = 2_000_000

#: 分档，用参考产品那套词。分档只是把分数读成人话，不参与计算。
BANDS = ((90, "极热"), (70, "热"), (50, "温"), (30, "冷启动"), (0, "静默"))


def weighted(metrics: dict | None) -> float | None:
    """一条帖子的加权互动量，或 None（这份材料没有互动读数）。"""
    if not metrics:
        return None
    total = 0.0
    seen = False
    for key, weight in WEIGHTS.items():
        value = metrics.get(key)
        if value is None:
            continue
        try:
            total += float(value) * weight
        except (TypeError, ValueError):
            continue
        seen = True
    return total if seen else None


def score(metrics: dict | None) -> float | None:
    """0-100，或 None。0 是一个读数（有人答了，答案是没人看），None 是没问到。

    刻度封顶在 100：再往上的分辨率在这一页没有用途（徽章只有五档），而原始数字一直跟在
    分数旁边，读者要看的是那几个数。
    """
    total = weighted(metrics)
    if total is None:
        return None
    if total <= 0:
        return 0.0
    return min(100.0, round(100.0 * math.log10(1.0 + total) / math.log10(1.0 + FULL_SCALE), 1))


def band(value: float | None) -> str:
    """分数读成人话；None 读作未判定，不是一个低的档。"""
    if value is None:
        return "未判定"
    for floor, label in BANDS:
        if value >= floor:
            return label
    return "静默"


def basis(metrics: dict | None) -> str:
    """分数旁边的原始数字，让读者自己判断这个分是怎么来的。"""
    if not metrics:
        return ""
    parts = []
    for key, label in (("views", "浏览"), ("likes", "赞"), ("retweets", "转"),
                       ("replies", "评")):
        value = metrics.get(key)
        if value is None:
            continue
        try:
            parts.append("%s %s" % (label, format(int(value), ",")))
        except (TypeError, ValueError):
            continue
    return " · ".join(parts)
