import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { VersionBadge } from "@/components/VersionBadge";

function renderBadge(version: string) {
  return render(
    <TooltipProvider>
      <VersionBadge version={version} />
    </TooltipProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("VersionBadge upgrade button", () => {
  it("有新版本时显示升级按钮,点击打开弹窗", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            tag_name: "v0.7.1",
            body: "### v0.7.1\n\n- something new",
            html_url: "https://github.com/zhoulingquan/Erza-Agent/releases/tag/v0.7.1",
          }),
      }),
    );
    renderBadge("0.7.0");

    // 版本号按钮与图标升级按钮共享同一个 aria-label,应同时出现两个
    const buttons = await screen.findAllByRole("button", { name: /New version/ });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[1]);
    await screen.findByText(/something new/);
  });

  it("已是最新时不显示升级按钮", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ tag_name: "v0.7.0", body: "" }),
      }),
    );
    renderBadge("0.7.0");

    await waitFor(() =>
      expect(screen.getByText("v0.7.0")).toBeInTheDocument(),
    );
    // 无更新时只有版本号按钮,不应出现图标升级按钮
    expect(screen.queryAllByRole("button", { name: /New version/ })).toHaveLength(0);
    expect(screen.getByText("v0.7.0")).toBeInTheDocument();
  });
});
