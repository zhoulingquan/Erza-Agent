import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ERZA_GITHUB_LATEST_RELEASE_URL,
  useVersionCheck,
} from "@/hooks/useVersionCheck";

function mockFetchOnce(payload: unknown, ok = true) {
  return vi.fn().mockResolvedValueOnce({
    ok,
    json: () => Promise.resolve(payload),
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useVersionCheck", () => {
  it("GitHub Release 优先:tag 更高时提示更新", async () => {
    const fetchMock = mockFetchOnce({
      tag_name: "v0.7.1",
      body: "### v0.7.1\n\n- something new",
      html_url: "https://github.com/zhoulingquan/Erza-Agent/releases/tag/v0.7.1",
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVersionCheck("0.7.0"));
    await waitFor(() => expect(result.current).not.toBeNull());

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(ERZA_GITHUB_LATEST_RELEASE_URL);
    expect(result.current?.hasUpdate).toBe(true);
    expect(result.current?.latestVersion).toBe("v0.7.1");
    expect(result.current?.releaseUrl).toContain("releases/tag/v0.7.1");
    expect(result.current?.notes.en).toContain("something new");
  });

  it("版本一致时不提示", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetchOnce({ tag_name: "v0.7.0", body: "", html_url: "" }),
    );
    const { result } = renderHook(() => useVersionCheck("0.7.0"));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current?.hasUpdate).toBe(false);
  });

  it("GitHub 404 时回退同源 updater.json", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 404 })
      .mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            version: "0.7.1",
            notes: { zh: "手动", en: "manual" },
            release_url: "https://example.com/r",
          }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVersionCheck("0.7.0"));
    await waitFor(() => expect(result.current).not.toBeNull());

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("/updater.json");
    expect(result.current?.hasUpdate).toBe(true);
    expect(result.current?.latestVersion).toBe("0.7.1");
  });

  it("两路都失败时静默无提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("network down")),
    );
    const { result } = renderHook(() => useVersionCheck("0.7.0"));
    // 给 effect 一次事件循环机会,确认不抛且不设置 info
    await act(async () => {});
    expect(result.current).toBeNull();
  });
});
