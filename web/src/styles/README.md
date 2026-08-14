# 视觉

三层按加载顺序即优先级：`tokens.css`（两态颜色）、`proto.css`（布局）、`app.css`（覆盖）。颜色只从 `tokens.css` 取；这里是视觉的唯一真相，没有第二份设计源。

## 视觉（字体 / 颜色 / 圆角 / 图标 / 主题）

字号、颜色、圆角、图标各只有一套来源，新控件沿用现有 token 即可；唯一的例外是运行点，登记在圆角那一条里。

| 规则 | 说明 / 反例 |
| --- | --- |
| 字体全站只用 sans：`--mono` 与 `--serif` 都是 `var(--sans)`，所以任何地方写 `var(--mono)` 都是 no-op —— 想让 CA 恢复等宽，改这一行即可；等宽原本承担的对齐交给 `tabular-nums`。**字号只有固定几档**，按钮与表单控件一档、表格正文一档、块标题三层。字重只用 400 / 500 / 600。 | 写入字号表之外的尺寸，之后改动它就没有依据；CSS 不写字重就落回浏览器默认 700，会出现「同为小标题、字重不同」。 |
| 颜色只有一条来源 `tokens.css`，页面里不写字面 hex：文字用 `--fg` / `--fg-2` / `--muted`；`--accent` **只做填充与边框，文字用 `--accent-fg`**；状态文字用 `--ok-fg` / `--warn-fg` / `--danger-fg`。 | 组件里写一个十六进制色，浅色主题下就是看不清的深色。 |
| 直角是全局的（`*{border-radius:0!important}`），新控件不应再引入圆角；原生控件内部不响应 `border-radius`。**例外只有一个，要写 `!important` 才能覆盖全局规则，并且必须在此登记**：运行点（`50%`）。卡片图标位曾是第二个（`6px`），2026-10-06 用户指定改回直角。 | 为第三个控件额外添加圆角即构成漂移 —— 除非用户指定，并且同时补登此处。 |
| 图标只有一套：`lucide-react` ＋ 组件 `Icon.tsx` ＋ 一条规则 `.ico{stroke-width:1.5;stroke-linecap:square;stroke-linejoin:miter}`。**几何在样式表里覆盖 lucide 的默认**（24 格、描边 2、圆角端点）—— `stroke-*` 是继承属性、样式表声明赢过表现属性，所以后加的图标不可能漏掉。 | 直接在组件里用 lucide 原组件就会混进圆角端点与 2px 描边。 |
| 两套 token 都在 `tokens.css`，持久化只写 `ch.theme` 一个键；`index.html` 里一段内联脚本在首屏渲染前设定好 `data-theme`。**浅色不是反色**：右区与左栏整体互换 —— 左栏跟 `--surface`，**报告卡片也改为跟随 `--surface`**，没有一处写死颜色。 | 只在 React 里设主题，深色模式下每次刷新会先闪一帧白；写反了就是右侧一片灰、视觉重心落在侧栏上。 |
| **新页面要加入共享的选择器，而不是自带一套外观**：收藏 / 搜索 / 系统设置的顶栏是 `#view-favorites .fav-bar,#view-lookup .fav-bar,#view-system .fav-bar` 那一条；新机会用和雷达相同的 `.topbar`（报告 / 设定两态，底线由 `.topbar::after` 内缩 16px）；同一页的 `.note-bar` 用 `#view-lookup>.note-bar,#view-opportunity>.note-bar{margin:12px 16px}`；带面板的列表页（收藏 / 搜索 / 新机会）用同一条轨道规则 `#view-favorites .fav-body,#view-lookup .look-body,#view-opportunity .fav-body{grid-template-columns:320px repeat(auto-fill,minmax(345px,1fr))}`。漏一处，那一页的顶栏就退回 `display:block` —— 标题与按钮堆成两行、没有 12px 内边距、没有下边框、也不吸顶（新机会页 2026-10-18 就是这样，实测 49px vs 51px）。 | 页面各自复制一份顶栏样式，改一次就漏一页；而"看起来差不多"在行高与吸顶上会露馅。 |
| **控件只有既有的那几种，语气用 `-<tone>` 后缀**：一个多选一用分段控件 `.tabs` / `.tab`（雷达的 报告 / 设定 就是它），一个标签用 `.chip` / `.chip-<tone>`，一个状态用 `.pill` / `.pill-<tone>`（`ok` / `warn` / `danger` / `muted`）。**不新造形状与颜色**：`pill warn` 不是类（写错了就是没有颜色），独立圆角 pill 也不是这个仓库的形状（圆角例外只有运行点）。 | 新造一个控件要同时新造 hover、选中态、浅色主题三样东西，而它们已经有了；语气写成 `pill warn` 会静默退化成灰字，肉眼很难发现。 |
| **`align-items:start` 是全站的，但有一条按页面的例外**：`.grid{align-items:start}`（app.css，2026-09-26：拉伸＋页脚 `margin-top:auto` 会在短卡页脚上方留下一条空带）适用于所有网格；新机会页用 `#view-opportunity .fav-body .grid{align-items:stretch}` 覆盖它，因为那一页的正文是**固定高度的块**，多出来的空间落在页脚之前的底部留白，不会插进正文中间。 | 把例外写成全站规则，就会把 2026-09-26 那条决定撤销；而"看起来一样"的两种空白（底部留白 vs 正文中间的空带）在别的页面上是两回事。 |
| **一块可变的块，两处共用一套规则**：报告卡的结论（`--verdict-lines:10`）与新机会卡的正文（`--opp-body-lines:7`）用的是同一套 `.verdict-more` / `.opp-body-more` 选择器与同一颗按钮外观；行数是各自唯一的数字。新机会那块用**固定 `height`** 而不是 `-webkit-line-clamp`，因为正文里有 `<ol>` 与 `<p>`，而 clamp 只对单一内联流精确。 | 两处各写一套按钮样式，改一次就漏一处；用 clamp 夹一个多块容器，被夹的位置取决于浏览器怎么盒化那些子块。 |
| **标签必须待在它的容器里**：`.chip` 的居中与 20px 高度写在 `.chips .chip`（proto.css），容器是 `.srow-v chips`（报告卡的 chips 就是这么写的）。裸放一个 `.chip` 会落回 app.css 那个 26px、`line-height:1` 的版本 —— 文字顶在框顶，看着像一个空盒子（新机会页 2026-10-18 实测，user 一眼看出）。 | 同名的两档：app.css 的 `.chip` 是分段条那一档，proto.css 的 `.chips .chip` 才是标签。少一层容器，形状就换了。 |
