import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** 导航条最大宽度(px)。圆点居中排列,点少时聚在中间向两边扩展,点多时压缩但不超过此宽度。 */
const CONTAINER_WIDTH = 192; // 对应 w-48
/** 相邻圆点中心最大间距(px)。超过则留白,保证少点时居中而不是被顶到两端。 */
const MAX_PITCH = 16;

interface ThreadNavDotsProps {
  /** 滚动容器 ref(外层 overflow-y-auto 元素)。 */
  scrollRef: React.RefObject<HTMLDivElement | null>;
  /** 可见用户消息 id 列表(按时间顺序)。 */
  userMessageIds: string[];
  /** 已加载但被分页隐藏的用户消息数量,用于在 tooltip 中显示全局序号。 */
  hiddenUserMessageCount?: number;
  /** 用户消息 id -> 文本内容,用于 hover tooltip 显示消息预览。 */
  userMessagePreviews?: Map<string, string>;
}

/**
 * 顶部横点导航条:每个圆点对应用户的一次输入。
 *
 * - 固定宽度容器,不限圆点个数,全部显示
 * - 整条居中:点少时聚在中间向两边对称扩展,点多时压缩间距但总宽不超容器
 * - 圆点尺寸与间隔随总数反比缩放(点数越多越小越紧凑)
 * - 点击圆点滚动定位到对应消息
 * - 当前可见区域内的圆点高亮
 * - hover 时显示 tooltip(消息文本预览)
 */
export function ThreadNavDots({
  scrollRef,
  userMessageIds,
  hiddenUserMessageCount = 0,
  userMessagePreviews,
}: ThreadNavDotsProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);

  const total = userMessageIds.length;

  // 根据 total 动态计算圆点尺寸(px)。点数越多尺寸越小,有上下限。
  const dotSize = useMemo(() => {
    if (total <= 1) return 8;
    const slotWidth = CONTAINER_WIDTH / total;
    // 圆点占 slot 宽度的约 45%,限制在 2~8px 之间
    return Math.min(8, Math.max(2, Math.round(slotWidth * 0.45)));
  }, [total]);

  // 每个圆点按钮占据的 slot 宽度(px),封顶后整条宽度 = total * pitch ≤ 容器宽
  const slotWidth = total > 0 ? Math.min(CONTAINER_WIDTH / total, MAX_PITCH) : 0;

  // 通过 scroll 事件计算当前可见的 user message
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || userMessageIds.length === 0) return;

    const computeActive = () => {
      const viewportTop = el.scrollTop;
      const viewportMid = viewportTop + el.clientHeight * 0.4;
      // 查找当前视口中线之上最后一条用户消息
      let bestIdx = 0;
      let bestTop = -Infinity;
      for (let i = 0; i < userMessageIds.length; i += 1) {
        const node = el.querySelector<HTMLElement>(
          `[data-user-turn="${userMessageIds[i]}"]`,
        );
        if (!node) continue;
        const top = node.offsetTop;
        if (top <= viewportMid && top > bestTop) {
          bestTop = top;
          bestIdx = i;
        }
      }
      setActiveIndex(bestIdx);
    };

    computeActive();
    el.addEventListener("scroll", computeActive, { passive: true });
    return () => el.removeEventListener("scroll", computeActive);
  }, [scrollRef, userMessageIds]);

  const scrollToUserMessage = useCallback(
    (msgId: string) => {
      const el = scrollRef.current;
      if (!el) return;
      const node = el.querySelector<HTMLElement>(`[data-user-turn="${msgId}"]`);
      if (!node) return;
      el.scrollTo({ top: node.offsetTop - 16, behavior: "smooth" });
    },
    [scrollRef],
  );

  const handleClick = useCallback(
    (index: number) => {
      const msgId = userMessageIds[index];
      if (msgId) scrollToUserMessage(msgId);
    },
    [userMessageIds, scrollToUserMessage],
  );

  const allDots = useMemo(
    () =>
      userMessageIds.map((id, index) => {
        const raw = userMessagePreviews?.get(id) ?? "";
        // 折叠空白并截断,避免 tooltip 过长
        const preview = raw.replace(/\s+/g, " ").trim().slice(0, 80);
        return {
          id,
          index,
          globalIndex: index + hiddenUserMessageCount + 1,
          preview,
        };
      }),
    [userMessageIds, hiddenUserMessageCount, userMessagePreviews],
  );

  if (allDots.length <= 1) return null;

  return (
    <div
      ref={containerRef}
      className="pointer-events-none flex w-full items-center justify-center"
      aria-label={t("thread.navDots", { defaultValue: "消息导航" })}
    >
      <div
        className="pointer-events-auto flex flex-row items-center justify-center"
        style={{ width: total * slotWidth }}
      >
        {allDots.map((dot) => {
          const isActive = dot.index === activeIndex;
          const isHovered = dot.index === hoveredIndex;
          return (
            <button
              key={dot.id}
              type="button"
              onClick={() => handleClick(dot.index)}
              onMouseEnter={() => setHoveredIndex(dot.index)}
              onMouseLeave={() => setHoveredIndex(null)}
              className="group relative flex items-center justify-center opacity-40 transition-opacity hover:opacity-80 data-[active=true]:opacity-100"
              style={{ width: slotWidth, height: dotSize + 6 }}
              data-active={isActive}
              aria-label={t("thread.goToMessage", {
                index: dot.globalIndex,
                defaultValue: "跳转到第 {{index}} 条消息",
              })}
            >
              {/* tooltip */}
              {isHovered && (
                <span
                  className={cn(
                    "pointer-events-none absolute bottom-full mb-2 left-1/2 -translate-x-1/2 max-w-[12rem] rounded-md",
                    "bg-foreground px-2 py-1 text-[11px] font-medium text-background",
                    "shadow-sm truncate block",
                  )}
                >
                  {dot.preview || `#${dot.globalIndex}`}
                </span>
              )}
              <span
                className="rounded-full bg-foreground transition-all"
                style={{
                  width: dotSize,
                  height: dotSize,
                  opacity: isActive ? 1 : isHovered ? 0.7 : 0.4,
                }}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}
