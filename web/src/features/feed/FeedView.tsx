import { CornerUpLeft, Newspaper } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useFeedTaxonomy } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { DailyView } from "./DailyView";
import { HotPage } from "./HotPage";
import { ItemPage } from "./ItemPage";
import { ItemsPage } from "./ItemsPage";
import { StoryPage } from "./StoryPage";
import { TopicsView } from "./TopicsView";
import { readPref, savePref } from "../../prefs";
import { useReadSet } from "./read";
import { word } from "./bits";
import type { Labels } from "./bits";
import type { FeedKind } from "./daily";

/** 信息流: 行业的宏观面 —— 大家在说什么, 而不是某条链上的币在做什么.
 *
 *  它是自己的一个地方 (Rail 的「信息流」三行), 不挂在任何雷达下面: 一条链的币和"这个行业在
 *  说什么"不是同一类东西, 所以它既不吃选中的链, 也不吃一套设定.
 *
 *  前三页共用这一个壳, 因为它们问的是同一批条目的三个问题, 页面这一层不重算任何一条: 精选的
 *  门槛是分数, 精选与全部动态只差后端一个 `scope`, 热点榜的次序是热度. 三个都是后端的
 *  (CLAUDE.md 2.3.4). 页面认识的只有排版.
 *
 *  几个地方由 Rail 决定, 所以这里没有第二排页签 —— 同一件事有两个能改的地方, 迟早会不一致.
 *  「报刊」是唯一的例外, 因为日报 / 周报 / 月报不是三个地方: 它们是同一份版式的三个周期, 所以
 *  合成一个入口 (用户定的, 2026-10-03), 切换器换的是版期这一个参数, 不是页面.
 *
 *  详情不是第四个地方, 所以它不开新的一页: 标题栏通栏, 下面平分两栏 —— 左边列表, 右边详情
 *  (DESIGN.md 4.8). 读者点开的是"刚才那一行", 位置本身就在告诉他这一点; 换成整页跳走, 他就丢了来路.
 *
 *  右栏常驻, 没点开时就是空的: 空栏加一句提示, 那句话是编的; 而右栏一会儿有一会儿没有, 点一下
 *  卡片左边就重排一次, 读者会以为页面出了问题.
 *
 *  右栏里存的是**一条路径**, 不是一个格子. 报道属于一件事, 事里又列着报道, 点进去再点回来是
 *  读者很自然会走的路; 只存一个格子的话, 从事件点进报道之后"事件"那一层就没了, 返回只能把整栏
 *  关掉, 看上去像跳回了列表. 所以点开是压一层, 返回是弹一层, 只有一层时返回禁用 (用户定的,
 *  2026-10-03)；不看了是 Esc, 它一路清空 —— 返回管往上走, Esc 管关掉, 两个动作各管一件事.
 *
 *  但"点开"分两种, 只有右栏里点出来的才压层. 左栏列表点开的是**起点**: 列表就在旁边, 要换一条
 *  再点一行就行, 返回按钮送不了他去任何地方 —— 从前它会把上一条报道留在栈里, 于是从列表点开
 *  第二条时按钮写着"返回报道", 而读者根本没觉得自己去过另一页. 起点就是整条路径.
 *
 *  主题不是第五个地方, 而是同一批条目的另一个问法: 精选与全部动态按"这是什么"筛 (类别互斥),
 *  主题按"这牵了谁"筛 (实体可多选, 窗口也长得多). 它和报刊一样是三栏而不是两栏, 理由也一样 ——
 *  左边一栏目录 (哪条线 / 哪一期), 中间一栏内容, 右边一栏点开的详情; 第三栏是同一个 `Pane`.
 *
 *  报刊不是实时视图之一: 前几页每次请求按当前窗口重算, 报刊读的是**出过的刊** —— 一期是快照,
 *  什么时候打开都是同一份. 所以它不在这条路径栈上, 也不开详情: 那一页整块就是一张摊开的纸,
 *  版式与来路都归 DailyView 自己管.
 */

export type FeedMode = "curated" | "all" | "hot" | "papers" | "topics";

/** 右边那一格里开着的东西. 一条报道, 或者一件事 —— 两者都能从别的详情页里点进去. */
export type FeedTarget = { kind: "item" | "story"; id: string };

/** 四个地方的名字. 版式性的标题, 所以是页面的字, 不是后端发下来的词 (CLAUDE.md 2.3.3). */
const TITLES: Record<FeedMode, string> = {
  curated: "精选",
  all: "全部动态",
  hot: "热点榜",
  papers: "报刊",
  topics: "主题",
};

/** 右栏里开着的那一页叫什么. 和上面那三个一样是版式标题, 所以也是页面的字. 返回按钮会把它
 *  拼在「返回」后面 (「返回事件」/「返回报道」), 读者按下去之前就知道自己会落在哪一页. */
const KINDS: Record<FeedTarget["kind"], string> = { item: "报道", story: "事件" };

/** 右栏那条路径记在本地: 重新打开时回到上次读到的地方 (5.25).
 *  只留最后两层 —— 路径是"这一次点开的那几层", 不是一个历史. */
const TRAIL_KEY = "ch.feed.trail";
const TRAIL_CAP = 2;

function loadTrail(): FeedTarget[] {
  try {
    const raw = readPref(TRAIL_KEY);
    const held = raw ? (JSON.parse(raw) as unknown) : null;
    if (!Array.isArray(held)) return [];
    return held
      .filter((item): item is FeedTarget =>
        !!item && typeof item === "object"
        && ((item as FeedTarget).kind === "item" || (item as FeedTarget).kind === "story")
        && typeof (item as FeedTarget).id === "string")
      .slice(-TRAIL_CAP);
  } catch {
    // 存坏的值 = 没有记录, 不是页面挂掉.
    return [];
  }
}

/** `onOpenBoard`: 精选页顶上那一块「当前热点」的出口 —— 它指的那张榜是另一个地方 (热点榜),
 *  而"去哪个地方"是 Rail 与 App 的事, 所以由外面递进来, 这里不自己换页面. */
export function FeedView({ mode, onOpenBoard, topic, onOpenTopic }: {
  mode: FeedMode;
  onOpenBoard: () => void;
  /** 一标记点开时要落在哪一条线上 (主题页的入口在卡片的角标上). 空 = 不指定. */
  topic?: string;
  /** 卡片上点一个实体角标: 换到主题页并选中它. */
  onOpenTopic: (name: string) => void;
}) {
  // 报刊读哪一种刊. 它是这一页自己选的一个参数, 不是 Rail 上的一个地方: 三种刊共用一份版式,
  // 差的只有一期的长短和几个词 (daily.ts 按 kind 分的那几个).
  const [paperKind, setPaperKind] = useState<FeedKind>("day");
  // 类别筛选只属于列表那两页, 所以它是壳的状态: 从精选切到全部动态时读者选的那个类别留着,
  // 切回来还是他上次看的那一类.
  const [category, setCategory] = useState("");
  // 右栏的路径. 末尾那一项就是现在开着的那一页, 它前面的是返回能落回去的那几级.
  const [trail, setTrail] = useState<FeedTarget[]>(() => loadTrail());
  const bar = useFeedTaxonomy();
  const labels = bar.data?.labels;
  // 读过的那几条: 标题淡下去. 在哪一条上打开就记哪一条, 记在本地 (read.ts), 与后端无关.
  const { read, mark } = useReadSet();
  const current = trail.length > 0 ? trail[trail.length - 1] : null;
  // 上一级: 只有一级时没有它, 返回按钮就是禁用的.
  const parent = trail.length > 1 ? trail[trail.length - 2] : null;

  // 换一页就清空整条路径: 路径是"这一页上点开的那几层", 页面换了它们就没有来路. 换一种刊
  // 也算: 栏里开着的那一件事是上一份报纸上的, 而纸已经换成另一期了.
  // 第一次进来不清: 这条路径是上一回关掉页面时留下的, 恢复它就是
  // "回到上次读到的地方". 只有真的换了页才清 —— 否则恢复刚做完就被自己抹掉.
  const restored = useRef(true);
  useEffect(() => {
    if (restored.current) {
      restored.current = false;
      return;
    }
    setTrail([]);
  }, [mode, paperKind]);

  // 路径记在本地, 重新打开时回到上次读到的那一条.
  useEffect(() => {
    savePref(TRAIL_KEY, JSON.stringify(trail.slice(-TRAIL_CAP)));
  }, [trail]);

  // Esc 是不看了, 不是返回上一级: 它一路清空. 键盘是面板旁边唯一不用瞄准的退路.
  useEffect(() => {
    if (!current) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setTrail([]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current]);

  // 列表点开的算起点: 整条路径换成这一条, 返回按钮恒为灰.
  const start = (target: FeedTarget) => setTrail([target]);
  const startItem = (id: string) => {
    mark(id);
    start({ kind: "item", id });
  };
  const startStory = (id: string) => start({ kind: "story", id });

  // 右栏里点出来的才算往下走一层: 这一层离开就找不回来 (报道一关, 里面那行"所属事件"也跟着走).
  const dig = (target: FeedTarget) => {
    // 从右栏里点开的报道同样是"读过了": 已读看的是读者有没有打开过这一条, 不是从哪一栏打开的.
    if (target.kind === "item") mark(target.id);
    setTrail((path) => {
      const top = path[path.length - 1];
      // 已经开着的那一页再点一次不压第二层.
      if (top && top.kind === target.kind && top.id === target.id) return path;
      return [...path, target];
    });
  };
  const back = () => setTrail((path) => path.slice(0, -1));

  return (
    <div id="view-feed">
      <div className="fav-bar">
        <div className="fav-title">
          <Icon as={Newspaper} size={14} />
          <b>{TITLES[mode]}</b>
        </div>
      </div>
      {mode === "papers" ? (
        <div className="feed-body daily-body">
          {/* key 跟着刊走: 选中的那一期是日报的 id, 换成周报就没有意义, 与其记得清空, 不如让
              组件跟着刊重来一遍 (DailyView.tsx 顶上那一段). */}
          <DailyView key={paperKind} kind={paperKind} onKind={setPaperKind} onOpenStory={startStory} />
          <Pane current={current} parent={parent} labels={labels} onBack={back} onDig={dig}
                onOpenTopic={onOpenTopic} />
        </div>
      ) : mode === "topics" ? (
        <div className="feed-body topics-body">
          {/* 左栏是目录, 中栏是这条线 —— 两栏都在 TopicsView 里, 第三栏还是那个 `Pane`, 和报刊
              同一套排法. */}
          <TopicsView onOpenItem={startItem} initialTopic={topic} />
          <Pane current={current} parent={parent} labels={labels} onBack={back} onDig={dig}
                onOpenTopic={onOpenTopic} />
        </div>
      ) : (
      <div className="feed-body">
        <div className="feed-col">
          {mode === "hot"
            ? <HotPage onOpenStory={startStory} />
            : (
              <ItemsPage
                scope={mode}
                category={category}
                onCategory={setCategory}
                onOpenItem={startItem}
                onOpenStory={startStory}
                onOpenBoard={onOpenBoard}
                onOpenTopic={onOpenTopic}
                read={read}
              />
            )}
        </div>
        <Pane current={current} parent={parent} labels={labels} onBack={back} onDig={dig}
              onOpenTopic={onOpenTopic} />
      </div>
      )}
    </div>
  );
}

/** 右栏那一格. 三个实时视图和报刊共用它, 因为返回的规矩只有一条: 栏里点出来的压一层, 返回
 *  弹一层, 一级时禁用; 栏常驻, 没点开时不写字.
 *
 *  它不认识"起点" —— 从纸上、榜上、列表上点开的那一条由调用方整条换掉路径, 所以进到这里时
 *  永远只有一层. 抄一份给报刊, 改天动返回的规矩就只会动到一处.
 *
 *  `current` / `parent` 由调用方算好递进来 (Esc 那条 effect 也要用), 这里不再推一遍. */
function Pane({ current, parent, labels, onBack, onDig, onOpenTopic }: {
  current: FeedTarget | null;
  parent: FeedTarget | null;
  labels: Labels | undefined;
  onBack: () => void;
  onDig: (target: FeedTarget) => void;
  onOpenTopic: (name: string) => void;
}) {
  return (
    <aside className="feed-pane">
      {current ? (
        <>
          <div className="pane-bar">
            {/* 返回只说"往上走一层". 一级时没有上一级, 所以它是灰的 —— 那也是读者知道自己
                在顶层的唯一提示. 按钮上的名字是上一级的名字, 不是现在这一页的. */}
            <button type="button" className="btn sec pane-back" disabled={!parent} onClick={onBack}>
              <Icon as={CornerUpLeft} size={14} />
              {word(labels, "back")}{parent ? KINDS[parent.kind] : ""}
            </button>
            {/* 版式标题: 这一格开着的是一条报道还是一件事. */}
            <span className="pane-kind">{KINDS[current.kind]}</span>
          </div>
          <div className="pane-scroll">
            {current.kind === "item"
              ? <ItemPage id={current.id}
                          onOpenStory={(id) => onDig({ kind: "story", id })}
                          onOpenItem={(id) => onDig({ kind: "item", id })}
                          onOpenTopic={onOpenTopic} />
              : <StoryPage id={current.id} onOpenItem={(id) => onDig({ kind: "item", id })}
                          onOpenStory={(id) => onDig({ kind: "story", id })}
                          onOpenTopic={onOpenTopic} />}
          </div>
        </>
      ) : null}
    </aside>
  );
}
