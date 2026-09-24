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

  it.each(["0.7.0", "0.7.11", "0.11.11", "11.11.11"])(
    "版本号 %s 下三段间距恒定",
    async (ver) => {
      // 远端永远更新,保证红点+升级按钮出现
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          json: () =>
            Promise.resolve({ tag_name: "v99.0.0", body: "x" }),
        }),
      );
      const { container } = renderBadge(ver);
      await screen.findAllByRole("button", { name: /New version/ });

      // 版本号→升级按钮:外层 gap-3(12px),边对边,与文字宽度无关
      const cluster = container.querySelector("span.inline-flex.gap-3");
      expect(cluster).not.toBeNull();
      // 红点:绝对定位锚在版本号按钮边(-right-1.5),不随文字长度漂移
      const dot = container.querySelector("span[aria-hidden='true'].-right-1\\.5");
      expect(dot).not.toBeNull();
      // 升级按钮:固定 20x26,不压缩
      const upgradeBtn = container.querySelector("button.w-\\[26px\\]");
      expect(upgradeBtn).not.toBeNull();
      expect(upgradeBtn?.className).toContain("shrink-0");
    },
  );

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
