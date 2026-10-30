import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, testQueryClient } from "../../../tests/test-utils";
import KeyResourceAccess from "./KeyResourceAccess";

vi.mock("../networking", () => ({ getProxyBaseUrl: () => "", getGlobalLitellmHeaderName: () => "Authorization" }));

const catalog = {
  routing_enabled: false,
  items: [
    {
      id: "aws",
      name: "AWS Bedrock Claude",
      source_kind: "bedrock",
      enabled: true,
      models: ["claude-test", "claude-other"],
      allowed_models: ["claude-test", "claude-other"],
    },
    {
      id: "openai",
      name: "OpenAI",
      source_kind: "openai",
      enabled: true,
      models: ["gpt-test"],
      allowed_models: ["gpt-test"],
    },
  ],
};

const props = { accessToken: "test-session", userId: "test-owner", admin: true, models: [] };

beforeEach(() => testQueryClient.clear());
afterEach(() => vi.unstubAllGlobals());

describe("KeyResourceAccess", () => {
  it("resolves an unrestricted fixed-pool key to that pool's live authorized models", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog)));
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(
      <KeyResourceAccess {...props} metadata={{ retail_resource_policy: { mode: "fixed", pool_id: "aws" } }} />,
    );
    expect(await screen.findByText("claude-test")).toBeInTheDocument();
    expect(screen.getByText("claude-other")).toBeInTheDocument();
    expect(screen.getByText("AWS Bedrock")).toBeInTheDocument();
    expect(screen.queryByText("gpt-test")).not.toBeInTheDocument();
    expect(screen.getByText(/No additional model restriction/)).toBeInTheDocument();
    expect(fetcher.mock.calls[0][0]).toBe("/retail/admin/pools/catalog?user_id=test-owner");
  });

  it("intersects custom pool grants with the key model restriction", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog))));
    renderWithProviders(
      <KeyResourceAccess
        {...props}
        models={["claude-test"]}
        metadata={{ retail_resource_policy: { mode: "custom", grants: [{ pool_id: "aws", models: "*" }] } }}
      />,
    );
    expect(await screen.findByText("claude-test")).toBeInTheDocument();
    expect(screen.queryByText("claude-other")).not.toBeInTheDocument();
    expect(screen.queryByText("gpt-test")).not.toBeInTheDocument();
  });

  it("keeps the saved fixed policy visible on lookup failure without claiming all models are available", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })),
    );
    renderWithProviders(
      <KeyResourceAccess {...props} metadata={{ retail_resource_policy: { mode: "fixed", pool_id: "aws" } }} />,
    );
    expect(await screen.findByText("Resource access could not be verified")).toBeInTheDocument();
    expect(screen.getByText("Fixed pool")).toBeInTheDocument();
    expect(screen.queryByText("claude-test")).not.toBeInTheDocument();
  });
});
