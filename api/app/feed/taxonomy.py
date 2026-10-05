"""这个行业的分类体系: 类别、主题标签、实体名录。

一个词表, 三个用处: 页面上的筛选栏、卡片角标、以及"规则版阅读"据以归类的依据。
换行业时改这一个文件 —— 类别的 key 会进网址 (`?category=...`), 上线后不要再改;
标签与名录可以随时增减。

没有 LLM 的时候, 归类由本文件的 `classify` 按关键词做; 接了模型之后, 模型的输出
填的是同一组字段, 词表仍然决定"有哪些类别", 只是不再是唯一判据。
"""

from __future__ import annotations

#: 页面上的类别。顺序就是筛选栏的顺序。`fallback` 那一类兜底: 归不到别处的东西进它,
#: 所以它必须存在且只有一个。
CATEGORIES: list[dict] = [
    {"key": "protocol", "label": "协议", "section": "协议与主网",
     "guide": "主网、升级、硬分叉、版本发布、路线图"},
    {"key": "product", "label": "产品", "section": "产品与工具",
     "guide": "钱包、客户端、应用、API 与开发者工具的发布和更新"},
    {"key": "defi", "label": "DeFi", "section": "DeFi 与市场",
     "guide": "借贷、DEX、衍生品、稳定币、流动性、收益与清算"},
    {"key": "infra", "label": "基建", "section": "基建与扩容",
     "guide": "L2、跨链桥、预言机、数据可用性、客户端与质押设施"},
    {"key": "exchange", "label": "交易所", "section": "交易所与上币",
     "guide": "上币、下架、新市场、交割与流动性计划"},
    {"key": "funding", "label": "融资", "section": "资本与并购",
     "guide": "融资轮次、并购、基金、做市与金库"},
    {"key": "policy", "label": "监管", "section": "政策与监管",
     "guide": "立法、牌照、诉讼、执法与税务"},
    {"key": "security", "label": "安全", "section": "安全事件",
     "guide": "被盗、漏洞、攻击、暂停与追回"},
    {"key": "research", "label": "研报", "section": "研究与观点",
     "guide": "报告、论文、数据、访谈、人物观点与趋势评论"},
    {"key": "industry", "label": "行业", "section": "行业动态", "fallback": True,
     "guide": "公司经营、人事、合作、市场与基础设施等归不到别处的动态"},
]

#: 归类用的关键词。**顺序就是优先级**: 一条讲"桥被攻击"的东西先被 security 接住,
#: 不会因为文中提到 bridge 落进 infra。
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("security", ("exploit", "hack", "drained", "rug", "phishing", "被盗", "漏洞", "攻击",
                  "闪电贷", "钓鱼", "异常转出", "暂停充值", "暂停提现", "紧急暂停")),
    ("policy", ("sec charges", "regulation", "regulator", "bill", "lawsuit", "license",
                "监管", "法案", "牌照", "诉讼", "合规", "立法", "罚款", "传票", "税务",
                "committee", "证监会")),
    ("exchange", ("will list", "listing", "delist", "上币", "下架", "开放交易", "现货上线",
                  "合约上线", "交割", "新市场")),
    ("funding", ("raises", "series a", "series b", "seed round", "acquire", "acquisition",
                 "融资", "领投", "并购", "收购", "估值", "金库", "做市")),
    ("protocol", ("mainnet", "upgrade", "hard fork", "roadmap", "主网", "升级", "硬分叉",
                  "路线图", "v2 上线", "版本发布")),
    ("product", ("wallet", "sdk", "api", "app store", "钱包", "客户端", "开发者工具",
                 "浏览器", "测试版", "开放注册")),
    ("defi", ("defi", "lending", "dex", "tvl", "apy", "liquidat", "stablecoin", "perp",
              "借贷", "清算", "流动性", "稳定币", "收益", "衍生品", "永续", "swap",
              "存款", "赎回")),
    ("infra", ("layer 2", "l2", "rollup", "bridge", "oracle", "proposer", "sequencer",
               "cross-chain", "跨链", "扩容", "预言机", "验证者", "排序器", "数据可用性",
               "再质押", "restaking")),
    ("research", ("report", "research", "paper", "survey", "interview", "outlook",
                  "报告", "研报", "论文", "调查", "访谈", "观点", "评论", "趋势", "展望")),
]

#: 主题标签。可选, 一条可以命中多个。
TOPIC_TAGS: list[tuple[str, tuple[str, ...]]] = [
    ("稳定币", ("stablecoin", "usdt", "usdc", "稳定币", "储备")),
    ("L2", ("layer 2", "l2", "rollup", "rollup", "二层", "扩容方案")),
    ("跨链", ("cross-chain", "bridge", "跨链", "桥")),
    ("MEV", ("mev", "block builder", "构建者", "捆绑交易")),
    ("RWA", ("rwa", "real world asset", "代币化国债", "现实资产")),
    ("衍生品", ("perp", "future", "option", "永续", "合约交易", "期权")),
    ("借贷", ("lending", "borrow", "借贷", "存款利率")),
    ("质押", ("staking", "validator", "质押", "验证者")),
    ("再质押", ("restaking", "restake", "再质押")),
    ("钱包", ("wallet", "钱包", "账户抽象", "aa 钱包")),
    ("交易所", ("exchange", "listing", "交易所", "上币")),
    ("预言机", ("oracle", "预言机")),
    ("数据可用性", ("data availability", "da layer", "blob", "数据可用性")),
    ("RWA 支付", ("payment", "settlement", "支付", "结算", "清算网络")),
    ("AI×Crypto", ("ai agent", "inference", "ai ", "人工智能", "推理", "算力")),
    ("DePIN", ("depin", "物理基础设施", "节点网络")),
    ("NFT", ("nft", "数字藏品")),
    ("治理", ("governance", "proposal", "治理", "提案")),
    ("机构", ("institution", "etf", "custody", "机构", "托管", "基金")),
]

#: 实体名录。第一个词是页面上的写法, 后面是别名 (全部小写后按子串匹配)。
ENTITY_TAGS: list[tuple[str, tuple[str, ...]]] = [
    ("Ethereum", ("ethereum", "以太坊", "eth ", "eth 主网")),
    ("Solana", ("solana", "sol ")),
    ("Base", ("base 链", "base mainnet", "coinbase base", "base 网络")),
    ("Arbitrum", ("arbitrum", "arb ")),
    ("Optimism", ("optimism", "op mainnet", "op 链")),
    ("BNB Chain", ("bnb chain", "bsc", "币安链")),
    ("Sui", ("sui ",)),
    ("Robinhood", ("robinhood",)),
    ("Uniswap", ("uniswap",)),
    ("Aave", ("aave",)),
    ("Lido", ("lido",)),
    ("Circle", ("circle", "usdc")),
    ("Tether", ("tether", "usdt")),
    ("Coinbase", ("coinbase",)),
    ("Binance", ("binance", "币安")),
    ("Kraken", ("kraken",)),
    ("OKX", ("okx", "欧易")),
    ("Bybit", ("bybit",)),
    ("SEC", ("sec ", "证监会")),
    ("CFTC", ("cftc",)),
    ("BlackRock", ("blackrock", "贝莱德")),
    ("Visa", ("visa",)),
    ("Stripe", ("stripe",)),
    ("Pantera", ("pantera",)),
]

#: 实体名录按这三组上墙 (主题页左栏的目录). 分组只是**摆放**: 一个实体仍然属于 ENTITY_TAGS
#: 那一份名录, 分组不改它怎么匹配, 也不改它进不进卡片的角标, 只在页面上分个段落. 名单里的
#: 名字必须与 ENTITY_TAGS 对得上 —— 名字打错的那个实体会静默地不上墙 (没有第二处会报错),
#: 所以下面有一条测试盯着这件事.
ENTITY_GROUPS: list[dict] = [
    {"key": "chain", "name": "链与协议",
     "members": ("Ethereum", "Solana", "Base", "Arbitrum", "Optimism", "BNB Chain", "Sui",
                 "Uniswap", "Aave", "Lido")},
    {"key": "exchange", "name": "交易所",
     "members": ("Coinbase", "Binance", "Kraken", "OKX", "Bybit", "Robinhood")},
    {"key": "institution", "name": "机构与监管",
     "members": ("Circle", "Tether", "SEC", "CFTC", "BlackRock", "Visa", "Stripe", "Pantera")},
]

#: 标签的第一格必须出自这一份「分类标签」(AIHOT 的 CATEGORY_TAGS)。我们的类别与分类标签
#: 一一对应 —— 类别的 label 就是分类标签 —— 所以第一格能反推出 category, 页面筛选栏与卡片角标
#: 不会各说各话。
CATEGORY_TAGS: list[str] = [entry["label"] for entry in CATEGORIES]

#: 同义词 -> 词表里的写法 (AIHOT 的 TAG_SYNONYMS)。模型写口语、写缩写、写英文 key 时在这里
#: 归一; 白名单之外的一律丢掉 —— 漏进角标的词, 筛选栏筛不到它, 那是一个静默的洞。
TAG_SYNONYMS: dict[str, str] = {
    "defi": "DeFi", "去中心化金融": "DeFi",
    "infra": "基建", "基础设施": "基建", "基础设施与扩容": "基建",
    "l2": "L2", "layer 2": "L2", "layer2": "L2", "二层": "L2",
    "跨链桥": "跨链", "bridge": "跨链",
    "法规": "监管", "政策": "监管", "policy": "监管",
    "攻击": "安全", "漏洞": "安全", "安全事件": "安全", "security": "安全",
    "并购": "融资", "funding": "融资",
    "上市": "交易所", "exchange": "交易所", "listing": "交易所",
    "研究": "研报", "research": "研报", "研究报告": "研报",
    "protocol": "协议", "product": "产品", "产品发布": "产品", "产品更新": "产品", "协议升级": "协议",
    "bnb": "BNB Chain", "bsc": "BNB Chain", "币安链": "BNB Chain",
    "币安": "Binance", "okex": "OKX", "欧易": "OKX", "贝莱德": "BlackRock",
    "usdt": "Tether", "泰达": "Tether", "usdc": "Circle",
    "eth": "Ethereum", "以太坊": "Ethereum", "sol": "Solana",
}

_CATEGORY_BY_KEY = {item["key"]: item for item in CATEGORIES}
_FALLBACK = next(item["key"] for item in CATEGORIES if item.get("fallback"))


def category_label(key: str) -> str:
    """The category as the page prints it. Unknown keys print as the fallback's label."""
    return _CATEGORY_BY_KEY.get(key, _CATEGORY_BY_KEY[_FALLBACK])["label"]


def category_section(key: str) -> str:
    """Which section of a publication this category belongs to (几个类别可以共用一节)."""
    return _CATEGORY_BY_KEY.get(key, _CATEGORY_BY_KEY[_FALLBACK])["section"]


def classify(text: str) -> dict:
    """Rule-based reading of one item: its category, its topic tags, its entities.

    Returns keys and names, never sentences: what a category *means* to a reader is the page's
    label and a later reader's summary, and both of those live elsewhere (CLAUDE.md 2.3.3).
    """
    low = " " + text.lower() + " "
    category = _FALLBACK
    for key, words in _KEYWORDS:
        if any(word in low for word in words):
            category = key
            break
    tags = [name for name, words in TOPIC_TAGS if any(word in low for word in words)]
    entities = [name for name, words in ENTITY_TAGS if any(word in low for word in words)]
    return {"category": category, "tags": tags, "entities": entities}

#: 三类标签各一张名单, 归一之后按这三张表分桶. 名字大小写敏感 (词表怎么写就怎么印).
_TOPIC_NAMES = {name for name, _ in TOPIC_TAGS}
_ENTITY_NAMES = {name for name, _ in ENTITY_TAGS}
_CATEGORY_TAG_KEYS = {entry["label"]: entry["key"] for entry in CATEGORIES}
_ALLOWED_BY_LOWER = {name.lower(): name
                     for name in list(_CATEGORY_TAG_KEYS) + list(_TOPIC_NAMES) + list(_ENTITY_NAMES)}
_SYNONYMS_BY_LOWER = {key.lower(): value for key, value in TAG_SYNONYMS.items()}


def is_category(key: str) -> bool:
    """这一格是不是类别词表里的 key (模型写回来的 category 要过这道筛)."""
    return key in _CATEGORY_BY_KEY


def _resolve_tag(raw: object) -> str:
    """一个标签过白名单: 去掉 `#`、查同义词、查大小写. 认不出就回空串 (丢掉)."""
    tag = str(raw or "").strip().lstrip("#").strip()
    if not tag:
        return ""
    lower = tag.lower()
    return _SYNONYMS_BY_LOWER.get(lower) or _ALLOWED_BY_LOWER.get(lower) or ""


def normalize_tags(raw: object, *, fallback: str = "", limit: int = 6) -> dict:
    """模型的标签过一遍白名单 (AIHOT 的 normalizeTags).

    规则逐条对齐: 只认词表里的写法, 同义词先归一, 去重, 取前 `limit` 个; 分类标签排第一 —— 一个
    都没有就用 `fallback` (规则版读出来的类别, 确定性兜底, 不为这个再问一次模型).

    返回的三格与页面上的三个位置一一对应: `category` 是类别 key, `tags` 是主题写法, `entities`
    是实体写法 (没有的由 `writing` 从 subjects 那一边补).
    """
    items = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for value in items:
        for chunk in str(value or "").replace("，", ",").split(","):
            tag = _resolve_tag(chunk)
            if tag and tag not in names:
                names.append(tag)
    category = next((name for name in names if name in _CATEGORY_TAG_KEYS), "")
    topics = [name for name in names if name in _TOPIC_NAMES]
    entities = [name for name in names if name in _ENTITY_NAMES]
    if not category:
        category = fallback if is_category(fallback) else _FALLBACK
    room = max(0, limit - 1)
    return {"category": _CATEGORY_TAG_KEYS.get(category, category),
            "tags": topics[:room],
            "entities": entities[:room]}
