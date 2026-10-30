import { describe, expect, it } from "vitest";
import { menuGroups } from "@/components/leftnav";
import { legacyPageRedirectHref } from "./legacyPageRoutes";

const redirect = (query: string) => legacyPageRedirectHref(new URLSearchParams(query));

describe("legacyPageRedirectHref", () => {
  it("sends an old ?page= bookmark to the path route that replaced it", () => {
    expect(redirect("page=logs")).toBe("/ui/logs");
    expect(redirect("page=models")).toBe("/ui/models-and-endpoints");
    expect(redirect("page=model-pricing")).toBe("/ui/model-pricing");
    expect(redirect("page=llm-playground")).toBe("/ui/playground");
    expect(redirect("page=new_usage")).toBe("/ui/usage");
    expect(redirect("page=usage")).toBe("/ui/old-usage");
  });

  it("keeps the older aliases for renamed pages", () => {
    expect(redirect("page=api_ref")).toBe("/ui/api-reference");
    expect(redirect("page=api-reference")).toBe("/ui/api-reference");
    expect(redirect("page=claude-code-plugins")).toBe("/ui/skills");
  });

  it.each(["model-catalog", "model-pricing", "channels", "resource-pools", "wallet", "billing", "wallet-admin"])(
    "opens the existing retail dashboard route for %s",
    (page) => {
      expect(redirect(`page=${page}`)).toBe(`/ui/${page}`);
    },
  );

  it("keeps the selected channel when opening a legacy channel details link", () => {
    expect(redirect("page=channel-details&channel=channel-a")).toBe("/ui/channel-details?channel=channel-a");
  });

  it("forwards the remaining query params so the MCP env-var setup link still opens its form", () => {
    expect(redirect("page=mcp-servers&fill_env_vars=srv-1")).toBe("/ui/mcp-servers?fill_env_vars=srv-1");
    expect(redirect("fill_env_vars=srv-1&page=mcp-servers")).toBe("/ui/mcp-servers?fill_env_vars=srv-1");
  });

  it("keeps forwarded values encoded", () => {
    expect(redirect("page=mcp-servers&fill_env_vars=a%26b%3Dc")).toBe("/ui/mcp-servers?fill_env_vars=a%26b%3Dc");
  });

  it("returns null when there is no page param or the id is unknown", () => {
    expect(redirect("")).toBeNull();
    expect(redirect("login=success")).toBeNull();
    expect(redirect("page=does-not-exist")).toBeNull();
    expect(redirect("page=constructor")).toBeNull();
  });

  it("covers every sidebar page id with the route the sidebar itself links to", () => {
    const leaves = menuGroups
      .flatMap((group) => group.items.flatMap((item) => item.children ?? [item]))
      .filter((item) => !item.external_url);
    expect(leaves.length).toBeGreaterThan(30);
    for (const leaf of leaves) {
      expect(redirect(`page=${leaf.page}`), leaf.page).toBe(`/ui/${leaf.route ?? leaf.page}`);
    }
  });
});
