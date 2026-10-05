import { useState } from "react";

import type { FeedFace } from "../../api/client";

/** 一件事的参与者头像 (照 AIHOT 的 `features/hot/Faces.tsx`): 最近在说的几家叠成一摞.
 *
 *  后端给的就是最近几枚 (`faces`), 页面只负责画: 有图标画图标, 没有就画首字母 —— 一个空头像
 *  和一个白块在列表里长得一样, 而"谁在说这件事"正是这一叠要回答的.
 *
 *  叠着的那几枚用一圈底色隔开, 所以叠在一起也数得清. 名次卡与列表行共用这一个组件, 尺寸由
 *  调用方给.
 */
export function Faces({ faces, size = 22, max = 5 }: {
  faces: FeedFace[];
  size?: number;
  max?: number;
}) {
  const shown = faces.slice(0, max);
  if (shown.length === 0) return null;
  return (
    <span className="faces" title={shown.map((face) => face.name).join("、")}>
      {shown.map((face, index) => (
        <span className={"face" + (index ? " stack" : "")} key={face.source_id || face.name}>
          <FaceAvatar face={face} size={size} />
        </span>
      ))}
    </span>
  );
}

/** 一枚头像. 图挂了就退回首字母: 一块破图占着位置, 比没有图更糟. */
function FaceAvatar({ face, size }: { face: FeedFace; size: number }) {
  const [broken, setBroken] = useState(false);
  if (face.icon && !broken) {
    return (
      <img src={face.icon} alt={face.name} width={size} height={size} loading="lazy"
           decoding="async" onError={() => setBroken(true)}
           style={{ width: size, height: size }} />
    );
  }
  return (
    <i className="face-ini" aria-hidden="true"
       style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.46)) }}>
      {face.initial}
    </i>
  );
}