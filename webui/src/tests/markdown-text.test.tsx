import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MarkdownText, balanceUnclosedFences } from "@/components/MarkdownText";

const rendererSpy = vi.hoisted(() => vi.fn());

vi.mock("@/components/MarkdownTextRenderer", () => ({
  default: ({
    children,
    highlightCode,
  }: {
    children: string;
    highlightCode?: boolean;
  }) => {
    rendererSpy({ children, highlightCode });
    return (
      <div
        data-testid="markdown-renderer"
        data-highlight-code={String(highlightCode)}
      >
        {children}
      </div>
    );
  },
}));

const FULL = "hello world, this is a typewriter test";

/** 手动 mock rAF:happy-dom 自带的 rAF 不受 fake timers 控制,此处逐帧精确推进。 */
function mockRaf() {
  const originalRaf = window.requestAnimationFrame;
  const originalCaf = window.cancelAnimationFrame;
  let nextId = 0;
  let now = 0;
  const queue = new Map<number, FrameRequestCallback>();
  window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
    nextId += 1;
    queue.set(nextId, callback);
    return nextId;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = ((id: number) => {
    queue.delete(id);
  }) as typeof window.cancelAnimationFrame;
  return {
    runFrames(count: number) {
      for (let i = 0; i < count; i += 1) {
        const pending = [...queue.values()];
        queue.clear();
        if (pending.length === 0) return;
        now += 16;
        for (const callback of pending) callback(now);
      }
    },
    restore() {
      window.requestAnimationFrame = originalRaf;
      window.cancelAnimationFrame = originalCaf;
    },
  };
}

async function flushTransitions() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("MarkdownText", () => {
  it("reveals streaming text progressively frame by frame instead of popping", async () => {
    rendererSpy.mockClear();
    vi.useFakeTimers();
    const raf = mockRaf();
    try {
      const { rerender } = render(
        <MarkdownText streaming>hello</MarkdownText>,
      );
      await flushTransitions();

      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent("hello");
      expect(screen.getByTestId("markdown-renderer")).toHaveAttribute(
        "data-highlight-code",
        "false",
      );

      // 新内容到达后先被 80ms 提交节流按住,不直接整批弹出
      rerender(<MarkdownText streaming>{FULL}</MarkdownText>);
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent("hello");

      act(() => {
        vi.advanceTimersByTime(79);
      });
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent("hello");

      // 提交定时器触发:目标变为全文,但显示仍停在旧处等逐帧释放
      act(() => {
        vi.advanceTimersByTime(1);
      });
      await flushTransitions();
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent("hello");

      // 推进 1 帧:变长但不满(积压 33 → 步长 9,共 14 字)
      act(() => {
        raf.runFrames(1);
      });
      await flushTransitions();
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent(
        "hello world, t",
      );

      // 帧数给足后完整呈现,流式中不高亮代码
      act(() => {
        raf.runFrames(30);
      });
      await flushTransitions();
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent(FULL);
      expect(screen.getByTestId("markdown-renderer")).toHaveAttribute(
        "data-highlight-code",
        "false",
      );

      // 流结束:直接贴合全文并开启高亮
      rerender(<MarkdownText>{FULL}</MarkdownText>);
      await flushTransitions();
      expect(screen.getByTestId("markdown-renderer")).toHaveTextContent(FULL);
      expect(screen.getByTestId("markdown-renderer")).toHaveAttribute(
        "data-highlight-code",
        "true",
      );
    } finally {
      raf.restore();
      vi.useRealTimers();
    }
  });

  it("closes unclosed code fences for stable streaming display", () => {
    expect(balanceUnclosedFences("```js\nconst a = 1;")).toBe(
      "```js\nconst a = 1;\n```",
    );
    expect(balanceUnclosedFences("```js\nconst a = 1;\n```")).toBe(
      "```js\nconst a = 1;\n```",
    );
    expect(balanceUnclosedFences("plain text")).toBe("plain text");
  });
});
