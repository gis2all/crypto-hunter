import { cloneElement, useEffect, useRef, useState, type CSSProperties, type ReactElement, type ReactNode } from "react";

/** 进场 / 出场 (照 AIHOT 的 `components/ui/Presence.tsx`).
 *
 *  用 CSS keyframes 而不是动画库: 那个库每页要多下几十 KB, 而这里只要这一个动作.
 *
 *  要走的那个元素**先留着把出场播完**, 才真正卸掉 —— 于是"收"也有过程, 不是啪一下消失.
 *  首帧就在的元素不动画: 页面刚打开时满屏都在动, 读者会以为出了问题.
 *
 *  出场把 `grid-template-rows` 从 1fr 收到 0fr, 高度自然落到 0 (见 Collapse 那一层), 所以
 *  不需要量高度, 也不需要写死一个 px.
 */

type Animatable = ReactElement<{ className?: string; style?: CSSProperties }>;

export function Presence({ show, children, enter, exit, duration }: {
  show: boolean;
  /** 一个元素, 动画的类挂在它身上. */
  children: Animatable;
  enter: string;
  exit: string;
  /** 出场播多少毫秒之后才卸掉. */
  duration: number;
}) {
  const [phase, setPhase] = useState<"still" | "in" | "out" | "gone">(show ? "still" : "gone");
  const last = useRef<Animatable>(children);
  if (show) last.current = children;
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    if (show) {
      setPhase("in");
      return;
    }
    setPhase((prev) => (prev === "gone" ? prev : "out"));
    const timer = setTimeout(() => setPhase((prev) => (prev === "out" ? "gone" : prev)), duration);
    return () => clearTimeout(timer);
  }, [show, duration]);
  if (!show && phase === "gone") return null;
  const child = show ? children : last.current;
  const cls = !show ? exit : phase === "in" ? enter : "";
  return cloneElement(child, { className: `${child.props.className ?? ""} ${cls}`.trim() });
}

/** 展开 / 收起的一块: 打开到自然高度, 收到没有 (grid 行 0fr 到 1fr). */
export function Collapse({ open, children, duration = 240, className = "" }: {
  open: boolean;
  children: ReactNode;
  duration?: number;
  className?: string;
}) {
  return (
    <Presence show={open} enter="anim-collapse-in" exit="anim-collapse-out" duration={duration}>
      <div className={"collapse-grid" + (className ? " " + className : "")}
           style={{ "--anim-ms": `${duration}ms` } as CSSProperties}>
        <div className="collapse-inner">{children}</div>
      </div>
    </Presence>
  );
}