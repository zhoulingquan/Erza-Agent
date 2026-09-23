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

    const upgradeBtn = await screen.findByText("Upgrade");
    expect(upgradeBtn).toBeInTheDocument();

    fireEvent.click(upgradeBtn);
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
    expect(screen.queryByText("Upgrade")).not.toBeInTheDocument();
    expect(screen.queryByText("升级")).not.toBeInTheDocument();
  });
});
