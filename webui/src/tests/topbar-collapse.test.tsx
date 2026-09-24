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
  it("展开态显示品牌名/版本号,左侧 272px(切换键已移至侧边栏内部)", () => {
    stubVersionCheck();
    const { container } = render(
      <TopBar
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
    // 切换键已搬进侧边栏,顶栏不再渲染收起/搜索按钮
    expect(
      screen.queryByRole("button", { name: /收起侧边栏|Collapse sidebar/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /搜索会话|Search chats/ }),
    ).not.toBeInTheDocument();
  });

  it("折叠态仍显示品牌名/版本号,左侧不收成 56px", () => {
    stubVersionCheck();
    const { container } = render(
      <TopBar
        theme="light"
        onToggleTheme={() => {}}
        onToggleLanguage={() => {}}
        sidebarWidth={272}
        sidebarCollapsed
        version="0.7.0"
      />,
    );
    // 折叠后不再锁定 56px:logo/版本号/更新按钮保留,宽度按内容自适应
    expect(leftPane(container)?.style.width).not.toBe("56px");
    expect(container.textContent).toMatch(/Erza/);
    expect(screen.getByText("v0.7.0")).toBeInTheDocument();
    // 切换键/搜索键在侧边栏内部,不在顶栏
    expect(
      screen.queryByRole("button", { name: /搜索会话|Search chats/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /收起侧边栏|Collapse sidebar/ }),
    ).not.toBeInTheDocument();
  });
});
