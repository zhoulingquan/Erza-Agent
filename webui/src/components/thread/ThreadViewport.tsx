import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ArrowDown } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { ThreadNavDots } from "@/components/thread/ThreadNavDots";
import { isAgentActivityMember } from "@/components/thread/AgentActivityCluster";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

interface ThreadViewportProps {
  messages: UIMessage[];
  isStreaming: boolean;
  composer: ReactNode;
  emptyState?: ReactNode;
  scrollToBottomSignal?: number;
  conversationKey?: string | null;
  showScrollToBottomButton?: boolean;
  /** Called when the user clicks the rewind button under the N-th user message. */
  onRewind?: (userMessageIndex: number) => void;
  /** Called when the user clicks the retry button under an assistant reply. */
  onRetry?: (userMessageIndex: number) => void;
}

const NEAR_BOTTOM_PX = 48;
const DEFAULT_SCROLL_BUTTON_BOTTOM_PX = 192;
const SCROLL_BUTTON_COMPOSER_GAP_PX = 16;
/* 镜像跟随:单帧应用的高度变化上限(px)。小增量即时贴合,大跳变(图片加载等)分摊多帧,避免瞬移。 */
const MIRROR_MAX_PX_PER_FRAME = 160;
/* 连续多少帧高度无变化后停止镜像循环(流式结束约 0.5s 后收尾)。 */
const MIRROR_IDLE_FRAMES = 30;
export const INITIAL_HISTORY_WINDOW = 160;
export const HISTORY_WINDOW_INCREMENT = 120;

export function windowMessages(messages: UIMessage[], visibleCount: number): UIMessage[] {
  if (messages.length <= visibleCount) return messages;
  let start = Math.max(0, messages.length - visibleCount);
  while (
    start > 0
    && isAgentActivityMember(messages[start])
    && isAgentActivityMember(messages[start - 1])
  ) {
    start -= 1;
  }
  return messages.slice(start);
}

export function ThreadViewport({
  messages,
  isStreaming,
  composer,
  emptyState,
  scrollToBottomSignal = 0,
  conversationKey = null,
  showScrollToBottomButton = true,
  onRewind,
  onRetry,
}: ThreadViewportProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const composerDockRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastConversationKeyRef = useRef<string | null>(conversationKey);
  const pendingConversationScrollRef = useRef(true);
  const scrollFrameIdsRef = useRef<number[]>([]);
  /** 程序化滚动代际:新调用/用户打断时 +1,让已排队的跟随帧失效(netcatty stopScroll 等价)。 */
  const followGenRef = useRef(0);
  /** 镜像跟随循环的 raf id(同一时间最多一个,高度变化自动贴合,无需重启)。 */
  const mirrorFollowRafRef = useRef<number | null>(null);
  /** 程序化滚动进行中:此时 scroll 事件来自代码而非用户,不判定为"离开底部"。 */
  const isProgrammaticScrollRef = useRef(false);
  const restoreScrollAfterPrependRef =
    useRef<{ height: number; top: number } | null>(null);
  /** User scrolled away from the bottom; do not auto-yank until they return or we reset (new chat / send). */
  const userReadingHistoryRef = useRef(false);
  const [atBottom, setAtBottom] = useState(true);
  const [composerDockHeight, setComposerDockHeight] = useState(0);
  const [visibleMessageCount, setVisibleMessageCount] =
    useState(INITIAL_HISTORY_WINDOW);
  const visibleMessageCountRef = useRef(visibleMessageCount);
  const visibleCountByChatRef = useRef<Map<string, number>>(new Map());
  const hasMessages = messages.length > 0;
  const visibleMessages = useMemo(
    () => windowMessages(messages, visibleMessageCount),
    [messages, visibleMessageCount],
  );
  const hiddenMessageCount = messages.length - visibleMessages.length;
  // Number of user messages hidden above the visible window. The backend's
  // user message index is based on the full conversation, so we add this
  // offset to the visible-window index when invoking rewind/retry.
  const hiddenUserMessageCount = useMemo(() => {
    if (hiddenMessageCount <= 0) return 0;
    const hiddenSlice = messages.slice(0, hiddenMessageCount);
    return hiddenSlice.filter(
      (m) => m.role === "user" && m.kind !== "trace",
    ).length;
  }, [messages, hiddenMessageCount]);
  const userMessageIds = useMemo(
    () =>
      visibleMessages
        .filter((m) => m.role === "user" && m.kind !== "trace")
        .map((m) => m.id),
    [visibleMessages],
  );
  const userMessagePreviews = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of visibleMessages) {
      if (m.role === "user" && m.kind !== "trace") {
        map.set(m.id, m.content);
      }
    }
    return map;
  }, [visibleMessages]);
  const scrollButtonBottom = composerDockHeight > 0
    ? composerDockHeight + SCROLL_BUTTON_COMPOSER_GAP_PX
    : DEFAULT_SCROLL_BUTTON_BOTTOM_PX;

  const cancelScheduledBottomScroll = useCallback(() => {
    for (const id of scrollFrameIdsRef.current) {
      window.cancelAnimationFrame(id);
    }
    scrollFrameIdsRef.current = [];
  }, []);

  /** 停掉镜像跟随循环(打断/取代/卸载时用)。 */
  const stopMirrorFollow = useCallback(() => {
    if (mirrorFollowRafRef.current !== null) {
      window.cancelAnimationFrame(mirrorFollowRafRef.current);
      mirrorFollowRafRef.current = null;
    }
    isProgrammaticScrollRef.current = false;
  }, []);

  const scrollToBottomNow = useCallback((smooth = false) => {
    const el = scrollRef.current;
    const marker = bottomRef.current;
    const behavior: ScrollBehavior = smooth ? "smooth" : "auto";
    if (marker) {
      marker.scrollIntoView({ block: "end", behavior });
    } else if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior });
    }
    setAtBottom(true);
  }, []);

  const scrollToBottom = useCallback(
    (smooth = false, frames = 1, options?: { force?: boolean }) => {
      const force = options?.force ?? false;
      cancelScheduledBottomScroll();
      // 新调用取代旧调用,用户打断同理:已排队的帧直接失效。
      followGenRef.current += 1;
      stopMirrorFollow();
      const gen = followGenRef.current;
      const run = () => {
        if (!force && userReadingHistoryRef.current) return;
        if (gen !== followGenRef.current) return;
        scrollToBottomNow(smooth);
      };
      run();
      for (let i = 1; i < frames; i += 1) {
        const id = window.requestAnimationFrame(() => {
          if (!force && userReadingHistoryRef.current) return;
          if (gen !== followGenRef.current) return;
          scrollToBottomNow(smooth);
        });
        scrollFrameIdsRef.current.push(id);
      }
    },
    [cancelScheduledBottomScroll, scrollToBottomNow, stopMirrorFollow],
  );

  /** 用户滚轮/触摸/主动上滚时打断正在进行的程序化平滑滚动。 */
  const interruptProgrammaticScroll = useCallback(() => {
    cancelScheduledBottomScroll();
    followGenRef.current += 1;
    stopMirrorFollow();
  }, [cancelScheduledBottomScroll, stopMirrorFollow]);

  /**
   * 增量镜像跟随:每帧把内容高度的变化量原样加到 scrollTop 上,视口与内容锁死、
   * 相对静止——新文字只在底部生长,运动粒度即内容生长粒度,天然平滑。
   * 大跳变按每帧上限分摊;连续多帧无变化自动收尾。已在跟随则直接返回。
   */
  const mirrorFollowToBottom = useCallback(() => {
    if (userReadingHistoryRef.current) return;
    const el = scrollRef.current;
    if (!el || mirrorFollowRafRef.current !== null) return;
    const gen = followGenRef.current;
    isProgrammaticScrollRef.current = true;
    let lastHeight = el.scrollHeight;
    let carry = 0;
    let idleFrames = 0;
    const step = () => {
      // 被新调用/用户打断取代:直接退出。
      if (gen !== followGenRef.current) {
        stopMirrorFollow();
        return;
      }
      const dh = el.scrollHeight - lastHeight + carry;
      lastHeight = el.scrollHeight;
      carry = 0;
      if (Math.abs(dh) < 0.5) {
        idleFrames += 1;
        if (idleFrames >= MIRROR_IDLE_FRAMES) {
          el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
          stopMirrorFollow();
          setAtBottom(true);
          return;
        }
        mirrorFollowRafRef.current = window.requestAnimationFrame(step);
        return;
      }
      idleFrames = 0;
      const apply = Math.sign(dh) * Math.min(Math.abs(dh), MIRROR_MAX_PX_PER_FRAME);
      carry = dh - apply;
      el.scrollTop += apply;
      mirrorFollowRafRef.current = window.requestAnimationFrame(step);
    };
    mirrorFollowRafRef.current = window.requestAnimationFrame(step);
  }, [stopMirrorFollow]);

  const loadEarlierMessages = useCallback(() => {
    const el = scrollRef.current;
    if (el) {
      restoreScrollAfterPrependRef.current = {
        height: el.scrollHeight,
        top: el.scrollTop,
      };
    }
    userReadingHistoryRef.current = true;
    setAtBottom(false);
    interruptProgrammaticScroll();
    setVisibleMessageCount((count) =>
      Math.min(messages.length, count + HISTORY_WINDOW_INCREMENT),
    );
  }, [messages.length, interruptProgrammaticScroll]);

  const measureComposerDock = useCallback(() => {
    const el = composerDockRef.current;
    if (!el) return;
    const height = el.getBoundingClientRect().height || el.offsetHeight;
    setComposerDockHeight((current) =>
      Math.abs(current - height) < 1 ? current : height,
    );
  }, []);

  useEffect(() => {
    visibleMessageCountRef.current = visibleMessageCount;
  }, [visibleMessageCount]);

  useEffect(() => {
    if (!atBottom) return;
    // Instant jump: CSS scroll-smooth + behavior "auto" still animates in some
    // browsers; session switches and history hydration should never slide from top.
    scrollToBottom(false);
  }, [messages, atBottom, scrollToBottom]);

  useEffect(() => {
    if (scrollToBottomSignal <= 0) return;
    userReadingHistoryRef.current = false;
    // 发送后滑到底(netcatty:新内容跟随用 smooth,只有会话切换/首屏用 instant)。
    scrollToBottom(true);
  }, [scrollToBottomSignal, scrollToBottom]);

  useLayoutEffect(() => {
    if (lastConversationKeyRef.current === conversationKey) return;
    const prevKey = lastConversationKeyRef.current;
    if (prevKey) {
      visibleCountByChatRef.current.set(prevKey, visibleMessageCountRef.current);
    }
    lastConversationKeyRef.current = conversationKey;
    pendingConversationScrollRef.current = true;
    userReadingHistoryRef.current = false;
    setAtBottom(true);
    const savedCount = conversationKey
      ? visibleCountByChatRef.current.get(conversationKey)
      : undefined;
    setVisibleMessageCount(savedCount ?? INITIAL_HISTORY_WINDOW);
  }, [conversationKey]);

  useLayoutEffect(() => {
    const pending = restoreScrollAfterPrependRef.current;
    if (!pending) return;
    const el = scrollRef.current;
    restoreScrollAfterPrependRef.current = null;
    if (!el) return;
    const delta = el.scrollHeight - pending.height;
    el.scrollTop = pending.top + delta;
  }, [visibleMessages.length]);

  useLayoutEffect(() => {
    if (!pendingConversationScrollRef.current) return;
    if (!conversationKey) {
      pendingConversationScrollRef.current = false;
      scrollToBottom(false, 4);
      return;
    }
    scrollToBottom(false, 8);
    if (!hasMessages) return;
    pendingConversationScrollRef.current = false;
  }, [conversationKey, hasMessages, messages, scrollToBottom]);

  useLayoutEffect(() => {
    measureComposerDock();
  }, [composer, hasMessages, measureComposerDock]);

  useEffect(() => () => {
    cancelScheduledBottomScroll();
    stopMirrorFollow();
  }, [cancelScheduledBottomScroll, stopMirrorFollow]);

  useEffect(() => {
    const target = contentRef.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (userReadingHistoryRef.current) return;
      // 内容长高(流式输出)时增量镜像跟随:视口与内容锁死,天然平滑。
      mirrorFollowToBottom();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMessages, mirrorFollowToBottom]);

  useEffect(() => {
    const target = composerDockRef.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measureComposerDock());
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMessages, measureComposerDock]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const onScroll = () => {
      // 程序化滚动(跟随循环)产生的事件:视为仍在底部,不判定为用户离开。
      if (isProgrammaticScrollRef.current) {
        setAtBottom(true);
        return;
      }
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      const near = distance < NEAR_BOTTOM_PX;
      setAtBottom(near);
      userReadingHistoryRef.current = !near;
      // 主动离开底部:立刻松开跟随并作废已排队的滚动(netcatty:上滚即停)。
      if (!near) interruptProgrammaticScroll();
    };
    // 滚轮/触摸是用户接管意图:打断平滑跟随,粘性状态仍由后续 scroll 事件判定。
    const onUserIntent = () => interruptProgrammaticScroll();

    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onUserIntent, { passive: true });
    el.addEventListener("touchmove", onUserIntent, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onUserIntent);
      el.removeEventListener("touchmove", onUserIntent);
    };
  }, [interruptProgrammaticScroll]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
      {/* 消息容器:独立滚动区,底部止于圆点行上方,消息进不到对话框区域。 */}
      <div
        ref={scrollRef}
        className={cn(
          "min-h-0 flex-1 overflow-y-auto scroll-auto scrollbar-thin",
          "[&::-webkit-scrollbar]:w-1.5",
          "[&::-webkit-scrollbar-thumb]:rounded-full",
          "[&::-webkit-scrollbar-thumb]:bg-muted-foreground/30",
          "[&::-webkit-scrollbar-track]:bg-transparent",
        )}
      >
        {hasMessages ? (
          <div ref={contentRef} className="mx-auto w-full max-w-[64rem]">
            <div className="px-4 pb-8 pt-4">
              <div className="mx-auto w-full max-w-[49.5rem]">
                <ThreadMessages
                  messages={visibleMessages}
                  isStreaming={isStreaming}
                  hiddenMessageCount={hiddenMessageCount}
                  onLoadEarlier={loadEarlierMessages}
                  onRewind={onRewind}
                  onRetry={onRetry}
                  userMessageIndexOffset={hiddenUserMessageCount}
                />
              </div>
            </div>
          </div>
        ) : (
          <div ref={contentRef} className="mx-auto flex min-h-full w-full max-w-[72rem] flex-col px-4">
            <div className="flex w-full flex-1 items-center justify-center py-10 sm:py-12">
              <div className="flex w-full max-w-[44rem] flex-col items-center gap-6 -mt-36">
                {emptyState}
                <div className="w-full">{composer}</div>
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} aria-hidden className="h-px" />
      </div>

      {/* 输入区 footer:正常文档流排在消息容器下方,不再 sticky 覆盖消息。 */}
      {hasMessages ? (
        <div
          ref={composerDockRef}
          data-testid="thread-composer-dock"
          className="relative z-10 shrink-0"
        >
          {userMessageIds.length > 1 && (
            <ThreadNavDots
              scrollRef={scrollRef}
              userMessageIds={userMessageIds}
              hiddenUserMessageCount={hiddenUserMessageCount}
              userMessagePreviews={userMessagePreviews}
            />
          )}
          <div className="px-4 pb-3">
            {composer}
          </div>
        </div>
      ) : null}

      {showScrollToBottomButton && !atBottom && (
        <Button
          variant="outline"
          size="icon"
          onClick={() => scrollToBottom(true, 1, { force: true })}
          className={cn(
            /* 抬到输入区 footer 上方(输入框 + 工具栏 + 可选目标条)。 */
            "absolute left-1/2 z-20 h-8 w-8 -translate-x-1/2 rounded-full shadow-md",
            "bg-background/90 backdrop-blur",
            "animate-in fade-in-0 zoom-in-95",
          )}
          style={{ bottom: scrollButtonBottom }}
          aria-label={t("thread.scrollToBottom")}
        >
          <ArrowDown className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
