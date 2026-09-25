import {
  Suspense,
  lazy,
  memo,
  startTransition,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { cn } from "@/lib/utils";

interface MarkdownTextProps {
  children: string;
  className?: string;
  streaming?: boolean;
}

const loadMarkdownRenderer = () => import("@/components/MarkdownTextRenderer");
const LazyMarkdownRenderer = lazy(loadMarkdownRenderer);

const MemoizedMarkdownRenderer = memo(function MemoizedMarkdownRenderer({
  source,
  className,
  highlightCode,
}: {
  source: string;
  className?: string;
  highlightCode: boolean;
}) {
  return (
    <LazyMarkdownRenderer className={className} highlightCode={highlightCode}>
      {source}
    </LazyMarkdownRenderer>
  );
});

const SHORT_STREAM_COMMIT_MS = 80;
const MEDIUM_STREAM_COMMIT_MS = 140;
const LONG_STREAM_COMMIT_MS = 220;

/* 打字机渐显:每帧最少释放字数(按 UTF-16 计,代理对边界会自动修正)。 */
const TYPEWRITER_MIN_STEP = 2;
/* 按积压比例加速(0.25 = 每帧追掉四分之一积压,超大积压时快速赶上)。 */
const TYPEWRITER_CATCH_UP = 0.25;
/* 单帧释放上限,避免后台切回/大块到达时一次性全抖出来。 */
const TYPEWRITER_MAX_STEP = 64;

export function preloadMarkdownText(): void {
  void loadMarkdownRenderer();
}

/**
 * Lightweight markdown renderer mirroring agent-chat-ui: GFM + math via
 * ``remark-math`` / ``rehype-katex``, and fenced code blocks delegated to
 * ``CodeBlock`` for copy-to-clipboard and syntax highlighting.
 */
export function MarkdownText({
  children,
  className,
  streaming = false,
}: MarkdownTextProps) {
  const committedSource = useStreamingMarkdownSource(children, streaming);
  // 流式时在提交节流之上再加一层逐帧渐显:每批提交的新字在接下来几帧里
  // 从上到下流出来,而不是整批突然弹出。
  const revealedSource = useTypewriterReveal(committedSource, streaming);
  const renderedSource = streaming
    ? balanceUnclosedFences(revealedSource)
    : committedSource;
  const highlightCode = !streaming && committedSource === children;

  useEffect(() => {
    if (streaming) preloadMarkdownText();
  }, [streaming]);

  return (
    <Suspense
      fallback={
        <div
          className={cn(
            "whitespace-pre-wrap break-words leading-relaxed text-foreground/92",
            className,
          )}
        >
          {renderedSource}
        </div>
      }
    >
      <MemoizedMarkdownRenderer
        source={renderedSource}
        className={className}
        highlightCode={highlightCode}
      />
    </Suspense>
  );
}

function useStreamingMarkdownSource(source: string, streaming: boolean): string {
  const [renderedSource, setRenderedSource] = useState(source);
  const latestSourceRef = useRef(source);
  const renderedSourceRef = useRef(source);
  const timerRef = useRef<number | null>(null);

  const clearPendingCommit = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const commitSource = useCallback((next: string, urgent: boolean) => {
    if (renderedSourceRef.current === next) return;
    renderedSourceRef.current = next;
    if (urgent) {
      setRenderedSource(next);
      return;
    }
    startTransition(() => setRenderedSource(next));
  }, []);

  const scheduleCommit = useCallback(() => {
    if (timerRef.current !== null) return;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      commitSource(latestSourceRef.current, false);
    }, streamingCommitDelay(latestSourceRef.current.length));
  }, [commitSource]);

  latestSourceRef.current = source;

  useEffect(() => {
    latestSourceRef.current = source;
    if (!streaming) {
      clearPendingCommit();
      commitSource(source, true);
    }
  }, [clearPendingCommit, commitSource, source, streaming]);

  useEffect(() => {
    latestSourceRef.current = source;
    if (!streaming) return;
    scheduleCommit();
  }, [scheduleCommit, source, streaming]);

  useEffect(() => clearPendingCommit, [clearPendingCommit]);

  return renderedSource;
}

function streamingCommitDelay(length: number): number {
  if (length > 24_000) return LONG_STREAM_COMMIT_MS;
  if (length > 8_000) return MEDIUM_STREAM_COMMIT_MS;
  return SHORT_STREAM_COMMIT_MS;
}

/** 取 `full` 的前 `shownLength + step` 个字符,保证不在代理对中间切断。 */
function takePrefix(full: string, shownLength: number, step: number): string {
  if (shownLength + step >= full.length) return full;
  let end = shownLength + step;
  const last = full.charCodeAt(end - 1);
  // 末尾是前导代理项(lead surrogate)则回退一位,避免拆散 emoji 等字符
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  // 极端情况(step 恰好切在代理对中间且 shownLength 为 0):向前补足一对,保证有进展
  if (end <= shownLength) end = shownLength + 2;
  return full.slice(0, end);
}

/** 流式展示时补齐未闭合的代码围栏,避免半截 fence 让整段渲染闪烁。 */
export function balanceUnclosedFences(text: string): string {
  const fences = text.match(/```/g)?.length ?? 0;
  return fences % 2 === 1 ? `${text}\n\`\`\`` : text;
}

/**
 * 打字机渐显:把已提交的文本按帧匀速释放,新字从上到下流出来。
 * 到达速度永远 >= 网络到达速度时落后部分由积压比例加速追上, backlog 为 0 即停。
 * 非追加式替换(重发/回退)或流结束时直接贴合 full,不倒放。
 */
function useTypewriterReveal(target: string, streaming: boolean): string {
  const [displayed, setDisplayed] = useState(target);
  const displayedRef = useRef(target);
  const latestRef = useRef(target);
  const rafRef = useRef<number | null>(null);
  latestRef.current = target;

  useEffect(() => {
    latestRef.current = target;
    if (!streaming) {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      if (displayedRef.current !== target) {
        displayedRef.current = target;
        setDisplayed(target);
      }
      return;
    }
    if (!target.startsWith(displayedRef.current)) {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      displayedRef.current = target;
      setDisplayed(target);
      return;
    }
    if (rafRef.current !== null || displayedRef.current === target) return;
    if (typeof window.requestAnimationFrame !== "function") {
      // 无 rAF 环境:直接贴合,不渐显
      displayedRef.current = target;
      setDisplayed(target);
      return;
    }
    const tick = () => {
      rafRef.current = null;
      const full = latestRef.current;
      const shown = displayedRef.current;
      if (!full.startsWith(shown)) {
        displayedRef.current = full;
        setDisplayed(full);
        return;
      }
      const backlog = full.length - shown.length;
      if (backlog <= 0) return;
      const step = Math.min(
        TYPEWRITER_MAX_STEP,
        Math.max(TYPEWRITER_MIN_STEP, Math.ceil(backlog * TYPEWRITER_CATCH_UP)),
      );
      const next = takePrefix(full, shown.length, step);
      displayedRef.current = next;
      startTransition(() => setDisplayed(next));
      if (next !== full) {
        rafRef.current = window.requestAnimationFrame(tick);
      }
    };
    rafRef.current = window.requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [target, streaming]);

  useEffect(() => () => {
    if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
  }, []);

  return displayed;
}
