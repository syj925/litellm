import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import KeyResourceRouting from "./KeyResourceRouting";
import { ResourceRoutingPolicy } from "./resourceRoutingPolicy";

vi.mock("../networking", () => ({ getProxyBaseUrl: () => "", getGlobalLitellmHeaderName: () => "Authorization" }));

const catalog = {
  routing_enabled: true,
  items: [
    { id: "pool-a", name: "Pool A", enabled: true, models: ["alpha", "beta"], allowed_models: ["alpha"] },
    { id: "pool-b", name: "Pool B", enabled: true, models: ["alpha", "beta"], allowed_models: ["beta"] },
    { id: "disabled", name: "Disabled", enabled: false, models: ["alpha"], allowed_models: ["alpha"] },
  ],
};

function Harness({ initial = null, admin = false }: { initial?: ResourceRoutingPolicy | null; admin?: boolean }) {
  const [policy, setPolicy] = useState(initial);
  const [valid, setValid] = useState(false);
  return (
    <>
      <KeyResourceRouting
        accessToken="test-token"
        targetUserId="target-user"
        admin={admin}
        value={policy}
        onChange={setPolicy}
        onValidityChange={setValid}
      />
      <output aria-label="Selected resource policy">{JSON.stringify(policy)}</output>
      <button disabled={!valid}>Save key</button>
    </>
  );
}

function mount(initial: ResourceRoutingPolicy | null = null, admin = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness initial={initial} admin={admin} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

it("defaults enabled routing to automatic and uses the selected owner's catalog for admin creation", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog)));
  vi.stubGlobal("fetch", fetch);
  mount(null, true);
  await waitFor(() => expect(screen.getByRole("button", { name: "Save key" })).toBeEnabled());
  expect(screen.getByLabelText("Selected resource policy")).toHaveTextContent('{"mode":"auto"}');
  expect(fetch.mock.calls[0][0]).toBe("/retail/admin/pools/catalog?user_id=target-user");
});

it("keeps per-pool model selections paired and rejects an empty custom selection", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog))));
  mount();
  fireEvent.click(await screen.findByRole("radio", { name: /^Custom/ }));
  expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText("Use pool Pool A"));
  fireEvent.click(screen.getByLabelText("Pool A: alpha"));
  fireEvent.click(screen.getByLabelText("Use pool Pool B"));
  fireEvent.click(screen.getByLabelText("Pool B: beta"));
  expect(screen.queryByRole("checkbox", { name: "Pool B: alpha" })).not.toBeInTheDocument();
  expect(JSON.parse(screen.getByLabelText("Selected resource policy").textContent ?? "null")).toEqual({
    mode: "custom",
    grants: [
      { pool_id: "pool-a", models: ["alpha"] },
      { pool_id: "pool-b", models: ["beta"] },
    ],
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "Save key" })).toBeEnabled());
});

it("allows explicit legacy routing only while account pool routing is disabled", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...catalog, routing_enabled: false }))),
  );
  mount();
  fireEvent.click(await screen.findByRole("radio", { name: /^Custom/ }));
  fireEvent.click(screen.getByRole("radio", { name: "Legacy native routing" }));
  expect(screen.getByLabelText("Selected resource policy")).toHaveTextContent("null");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save key" })).toBeEnabled());
});

it("keeps disabled saved pools invalid rather than switching to a healthy pool", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog))));
  mount({ mode: "fixed", pool_id: "disabled" });
  expect(await screen.findByRole("alert")).toHaveTextContent("Choose an available account-authorized pool");
  expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
  expect(screen.getByLabelText("Selected resource policy")).toHaveTextContent('"pool_id":"disabled"');
});

it("blocks a saved policy when account authorization cannot be loaded", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })),
  );
  mount({ mode: "fixed", pool_id: "pool-a" });
  expect(await screen.findByRole("alert")).toHaveTextContent("Resource pools could not be verified");
  expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();
});
