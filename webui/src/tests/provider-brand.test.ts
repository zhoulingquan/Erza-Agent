import { describe, expect, it } from "vitest";

import { faviconUrls, logoFallbackUrls, providerBrand } from "@/lib/provider-brand";

const FAVICON_PROXIES = /icons\.duckduckgo\.com|google\.com\/s2\/favicons/;

describe("provider brand logos", () => {
  it("only relies on the first-party favicon", () => {
    expect(faviconUrls("z.ai")).toEqual(["https://z.ai/favicon.ico"]);
  });

  it("keeps explicit Google favicon URLs first before trying the site favicon", () => {
    expect(logoFallbackUrls("https://www.google.com/s2/favicons?domain=browserbase.com&sz=64")).toEqual([
      "https://www.google.com/s2/favicons?domain=browserbase.com&sz=64",
      "https://browserbase.com/favicon.ico",
    ]);
  });

  it("normalizes path-like favicon domains for secondary fallbacks", () => {
    expect(logoFallbackUrls("https://www.google.com/s2/favicons?domain=github.com/HKUDS/CLI-Anything&sz=64")).toEqual([
      "https://www.google.com/s2/favicons?domain=github.com/HKUDS/CLI-Anything&sz=64",
      "https://github.com/favicon.ico",
    ]);
  });

  it("keeps DeepSeek on its brand domain", () => {
    expect(providerBrand("deepseek")?.logoUrls[0]).toBe("https://deepseek.com/favicon.ico");
    expect(providerBrand("deepseek")?.initials).toBe("DS");
  });

  it("uses the official Agnes logo before favicon fallbacks", () => {
    const brand = providerBrand("agnes");
    expect(brand?.logoUrls[0]).toBe("https://agnes-ai.com/images/biglogo.png");
    expect(brand?.logoUrls).toContain("https://agnes-ai.com/favicon.ico");
  });

  it("uses official first-party assets for OpenCode", () => {
    expect(providerBrand("opencode")?.logoUrls[0]).toBe("https://opencode.ai/favicon.ico");
    expect(providerBrand("opencode")?.initials).toBe("OC");
  });

  it("never requests third-party favicon proxies", () => {
    for (const brand of ["agnes", "brave", "custom", "deepseek", "duckduckgo", "exa", "jina", "kagi", "olostep", "opencode", "tavily"]) {
      expect(providerBrand(brand)?.logoUrls.join("\n")).not.toMatch(FAVICON_PROXIES);
    }
  });
});
