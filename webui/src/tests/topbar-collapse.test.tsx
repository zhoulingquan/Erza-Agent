import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TopBar } from "@/components/thread/TopBar";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stubVersionCheck() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status: 404 }),
  );
}

function leftPane(container: HTMLElement): HTMLElement | null {
  return container.querySelector("header > div");
}

describe("TopBar sidebar collapse", () => {
  it("展开态显示品牌名/版本号/搜索,左侧 272px", () => {
    stubVersionCheck();
    const { container } = render(
      <TopBar
        onToggleSidebar={() => {}}
        onOpenSearch={() => {}}
        theme="light"
        onToggleTheme={() => {}}
        onToggleLanguage={() => {}}
        sidebarWidth={272}
        sidebarCollapsed={false}
        version="0.7.0"
      />,
    );
    expect(leftPane(container)?.style.width).toBe("272px");
    // 品牌名与版本号
    expect(container.textContent).toMatch(/Erza/);
    expect(screen.getByText("v0.7.0")).toBeInTheDocument();
    // 收起 + 搜索按钮都在
    expect(
      screen.getByRole("button", { name: /收起侧边栏|Collapse sidebar/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /搜索会话|Search chats/ }),
    ).toBeInTheDocument();
  });

  it("折叠态只剩收起按钮,左侧 56px", () => {
    stubVersionCheck();
    const { container } = render(
      <TopBar
        onToggleSidebar={() => {}}
        onOpenSearch={() => {}}
        theme="light"
        onToggleTheme={() => {}}
        onToggleLanguage={() => {}}
        sidebarWidth={272}
        sidebarCollapsed
        version="0.7.0"
      />,
    );
    expect(leftPane(container)?.style.width).toBe("56px");
    // 品牌名/版本号/搜索隐藏
    expect(container.textContent).not.toMatch(/Erza/);
    expect(screen.queryByText("v0.7.0")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /搜索会话|Search chats/ }),
    ).not.toBeInTheDocument();
    // 收起按钮(展开回去的入口)仍在
    expect(
      screen.getByRole("button", { name: /收起侧边栏|Collapse sidebar/ }),
    ).toBeInTheDocument();
  });
});
