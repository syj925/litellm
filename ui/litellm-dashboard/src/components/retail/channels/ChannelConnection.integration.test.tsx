import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChannelEditor from "./ChannelEditor";
import type { ChannelConnectionSummary, NamedChannel } from "./types";
import type { ResourcePool } from "../pools/types";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));

const pool: ResourcePool = {
  id: "pool-fixture",
  name: "Fixture pool",
  source_kind: "anthropic",
  description: "",
  enabled: true,
  visibility: "restricted",
  priority: 1,
  default_discount: "1",
  version: 1,
  bindings: [],
  models: ["model-a", "model-b"],
};
const channel: NamedChannel = {
  id: "channel-fixture",
  name: "Fixture channel",
  pool_id: pool.id,
  pool_name: pool.name,
  deployment_ids: ["a", "b"],
  models: ["model-a", "model-b"],
  deployment_count: 2,
  status: "active",
  version: 7,
};
const deployments = ["a", "b"].map((id) => ({
  deployment_id: id,
  model: "model-" + id,
  provider: "anthropic",
  credential_name: null,
  connection_id: "connection-fixture",
  endpoint_host: "current.example",
}));
const summary: ChannelConnectionSummary = {
  channel_id: channel.id,
  version: channel.version,
  deployment_count: 2,
  editable_deployment_count: 2,
  can_edit: true,
  reason: null,
  api_base: "https://current.example/prefix",
  api_base_mixed: false,
  has_api_key: true,
  credential_mode: "direct",
};

function mount(summaryResponse: Response, saveResponse?: Response, selectedPool = pool) {
  const writes: Record<string, unknown>[] = [];
  const fetch = vi.fn(async (path: RequestInfo | URL, options?: RequestInit) => {
    if (String(path).endsWith("/connection")) return summaryResponse;
    writes.push(JSON.parse(String(options?.body)));
    return saveResponse ?? new Response(JSON.stringify(channel));
  });
  vi.stubGlobal("fetch", fetch);
  const close = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ChannelEditor
        channel={channel}
        deployments={deployments}
        pools={[selectedPool]}
        accessToken="fixture-admin-token"
        onClose={close}
      />
    </QueryClientProvider>,
  );
  return { writes, close, fetch };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("channel connection editing", () => {
  it("prefills only the safe URL and sends a URL-only patch with Save channel", async () => {
    const user = userEvent.setup();
    const { writes, close } = mount(
      new Response(JSON.stringify({ ...summary, api_key: "stored-secret-must-not-render" })),
    );
    const base = await screen.findByDisplayValue(summary.api_base!);
    expect(screen.getByRole("combobox", { name: "Resource pool" })).toHaveTextContent(pool.name);
    expect(base).toHaveAttribute("readonly");
    const key = screen.getByLabelText("New API key");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveValue("");
    expect(screen.queryByDisplayValue("stored-secret-must-not-render")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Change API base URL" }));
    fireEvent.change(base, { target: { value: "https://replacement.example/custom/v1" } });
    fireEvent.change(key, { target: { value: " \t " } });
    expect(screen.getByText(/Saving applies connection changes to all 2 selected deployments/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save channel" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save channel" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(writes).toEqual([
      {
        name: channel.name,
        pool_id: pool.id,
        deployment_ids: ["a", "b"],
        expected_version: 7,
        connection_patch: { api_base: "https://replacement.example/custom/v1" },
      },
    ]);
  });

  it("updates the visible save scope and final patch targets when a member is removed from mixed URLs", async () => {
    const user = userEvent.setup();
    const { writes, close } = mount(new Response(JSON.stringify({ ...summary, api_base: null, api_base_mixed: true })));
    await screen.findByText("Mixed upstream URLs");
    fireEvent.change(screen.getByLabelText("New API key"), { target: { value: "fixture-replacement-key" } });
    expect(screen.getByText(/Saving applies connection changes to all 2 selected deployments/)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Select model-b b/ }));
    expect(screen.getByText(/Saving applies connection changes to all 1 selected deployments/)).toBeInTheDocument();
    expect(screen.getByText(/Removed models keep their current connection/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save channel" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save channel" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(writes[0].deployment_ids).toEqual(["a"]);
    expect(writes[0].connection_patch).toEqual({ api_key: "fixture-replacement-key" });
  });

  it("keeps credentials unchanged when the replacement value contains only whitespace", async () => {
    const user = userEvent.setup();
    const { writes, close } = mount(new Response(JSON.stringify(summary)));
    await screen.findByDisplayValue(summary.api_base!);
    fireEvent.change(screen.getByLabelText("New API key"), { target: { value: " \t " } });
    await user.click(screen.getByRole("button", { name: "Save channel" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(writes[0]).not.toHaveProperty("connection_patch");
  });

  it("does not enable connection editing after a failed summary load but still permits membership saving", async () => {
    const user = userEvent.setup();
    const { writes, close } = mount(
      new Response(JSON.stringify({ detail: "fixture-sensitive-upstream-value" }), { status: 503 }),
    );
    await screen.findByText("Connection settings could not be loaded");
    expect(screen.getByLabelText("New API key")).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Change API base URL" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByText("fixture-sensitive-upstream-value")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save channel" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(writes[0]).not.toHaveProperty("connection_patch");
  });

  it("keeps unsupported and stale connections read-only", async () => {
    const unsupported = { ...summary, can_edit: false, reason: "Static model configuration is read-only.", version: 8 };
    mount(new Response(JSON.stringify(unsupported)));
    await screen.findByText("Connection settings are read-only");
    expect(screen.getByText(/Close and reopen this channel/)).toBeInTheDocument();
    expect(screen.getByLabelText("New API key")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save channel" })).toBeEnabled();
  });

  it("shows the selected draft pool name before its options are opened", async () => {
    mount(new Response(JSON.stringify(summary)), undefined, { ...pool, enabled: false });
    await screen.findByDisplayValue(summary.api_base!);
    expect(screen.getByRole("combobox", { name: "Resource pool" })).toHaveTextContent(pool.name + " (draft)");
  });

  it("uses a fixed safe error message when a connection save fails", async () => {
    const user = userEvent.setup();
    const secret = "fixture-replacement-sensitive-key";
    const { close } = mount(
      new Response(JSON.stringify(summary)),
      new Response(JSON.stringify({ detail: "Invalid key: " + secret }), { status: 400 }),
    );
    await screen.findByDisplayValue(summary.api_base!);
    fireEvent.change(screen.getByLabelText("New API key"), { target: { value: secret } });
    await user.click(screen.getByRole("button", { name: "Save channel" }));
    expect(
      await screen.findByText(
        "The connection update was rejected. Review the URL, provider support and selected deployments.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Invalid key: " + secret)).not.toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();
  });
});
