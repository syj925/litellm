import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import PoolAccessEditor from "./PoolAccessEditor";
import PoolPreview from "./PoolPreview";
import PoolWizard from "./PoolWizard";
import ResourcePoolsPanel from "./ResourcePoolsPanel";
import type { PoolDeployment, ResourcePool } from "./types";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    accessToken: "test-admin",
    userId: "test-operator",
    userRole: "Admin",
    isAuthorized: true,
    isLoading: false,
  }),
}));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));

const poolA: ResourcePool = {
  id: "pool-a",
  name: "Source A",
  source_kind: "anthropic",
  description: "",
  enabled: true,
  visibility: "public",
  priority: 0,
  default_discount: "1",
  version: 1,
  models: ["alpha", "beta"],
  bindings: [{ deployment_id: "deployment-a", model: "alpha", weight: 1, priority: 0 }],
};
const poolB: ResourcePool = {
  ...poolA,
  id: "pool-b",
  name: "Source B",
  source_kind: "bedrock",
  visibility: "restricted",
};

function serveApi(deployments?: PoolDeployment[]) {
  const writes: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = new URL(input, "http://localhost").pathname;
      if (options?.method && options.method !== "GET") {
        const body: unknown = options.body ? JSON.parse(String(options.body)) : null;
        writes.push({ path, body });
        return new Response(
          JSON.stringify(
            path.endsWith("preview")
              ? {
                  pool_id: "pool-a",
                  pool_name: "Source A",
                  allowed_deployment_ids: ["deployment-a"],
                  bindings: poolA.bindings,
                  settings_version: 1,
                }
              : poolA,
          ),
          { status: 200 },
        );
      }
      const data = path.endsWith("deployments")
        ? {
            items: deployments ?? [
              {
                deployment_id: "deployment-a",
                model: "alpha",
                provider: "anthropic",
                credential_name: "Test credentials",
              },
              { deployment_id: "deployment-b", model: "beta", provider: "anthropic", credential_name: null },
            ],
          }
        : path.endsWith("settings")
          ? { routing_enabled: false, version: 1, unassigned_models: ["unassigned-model"] }
          : path.endsWith("pool-access")
            ? { mode: "inherit", grants: [], effective_grants: [{ pool_id: "pool-a", models: "*" }] }
            : { items: [poolA, poolB] };
      return new Response(JSON.stringify(data), { status: 200 });
    }),
  );
  return writes;
}

function mount(component: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}>{component}</QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

it("saves reviewed source, bindings, access, and pricing in one atomic publish request", async () => {
  const writes = serveApi();
  mount(
    <PoolWizard
      accessToken="test-admin"
      seed={{ name: "AWS Bedrock Claude", source_kind: "bedrock" }}
      onClose={vi.fn()}
    />,
  );
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^Existing unnamed deployments/ }));
  await user.click(await screen.findByRole("checkbox", { name: "Bind alpha deployment deployment-a" }));
  await user.click(screen.getByRole("checkbox", { name: "Bind beta deployment deployment-b" }));
  await user.click(screen.getByRole("button", { name: "Advanced routing weights and priorities" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Weight for alpha deployment-a" }), { target: { value: "3" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Public pool" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Default percentage of reference price (%)" }), {
    target: { value: "70.1234567" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Save and publish" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/pools",
        body: {
          id: "aws-bedrock-claude",
          name: "AWS Bedrock Claude",
          source_kind: "bedrock",
          description: "",
          priority: 0,
          enabled: true,
          visibility: "public",
          default_discount: "0.701234567",
          bindings: [
            { deployment_id: "deployment-a", model: "alpha", weight: 3, priority: 0 },
            { deployment_id: "deployment-b", model: "beta", weight: 1, priority: 0 },
          ],
        },
      },
    ]),
  );
});

it("shows same-name models under explicit channel names and locks channel membership to its assigned pool", async () => {
  const writes = serveApi([
    {
      deployment_id: "deployment-a",
      model: "alpha",
      provider: "anthropic",
      credential_name: null,
      channel_id: "official",
      channel_name: "Official production",
      pool_id: "pool-a",
      pool_name: "Source A",
    },
    {
      deployment_id: "deployment-b",
      model: "beta",
      provider: "anthropic",
      credential_name: null,
      channel_id: "official",
      channel_name: "Official production",
      pool_id: "pool-a",
      pool_name: "Source A",
    },
    {
      deployment_id: "deployment-c",
      model: "alpha",
      provider: "anthropic",
      credential_name: null,
      channel_id: "cloud",
      channel_name: "Cloud production",
      pool_id: "pool-b",
      pool_name: "Source B",
    },
  ]);
  const pool = {
    ...poolA,
    bindings: [
      { deployment_id: "deployment-a", model: "alpha", weight: 7, priority: 3 },
      { deployment_id: "deployment-b", model: "beta", weight: 1, priority: 0 },
    ],
  };
  mount(<PoolWizard accessToken="test-admin" pool={pool} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  const official = await screen.findByRole("group", { name: "Official production" });
  const cloud = screen.getByRole("group", { name: "Cloud production" });
  expect(within(official).getByRole("checkbox", { name: "Channel Official production" })).toBeChecked();
  expect(within(official).getByRole("checkbox", { name: "Channel Official production" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(within(cloud).getByRole("checkbox", { name: "Channel Cloud production" })).not.toBeChecked();
  expect(within(cloud).getByRole("checkbox", { name: "Channel Cloud production" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(within(cloud).getByText("Source B")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Manage channels" })).toHaveAttribute("href", "/ui/channels");
  fireEvent.change(screen.getByRole("textbox", { name: "Search upstream deployments" }), { target: { value: "beta" } });
  expect(screen.queryByRole("group", { name: "Cloud production" })).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(within(official).getByRole("button", { name: "View models" }));
  expect(within(official).getByText("alpha")).toBeInTheDocument();
  expect(within(official).getByText("beta")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Save published pool" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({ path: "/retail/admin/pools/pool-a", body: { bindings: pool.bindings } });
});

it.each([
  { button: "Save draft", enabled: false },
  { button: "Save and publish", enabled: true },
])("creates an empty pool through $button so a channel can be assigned later", async ({ button, enabled }) => {
  const writes = serveApi();
  const onClose = vi.fn();
  mount(
    <PoolWizard accessToken="test-admin" seed={{ name: "Anthropic", source_kind: "anthropic" }} onClose={onClose} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  expect(screen.getByText(/It cannot serve requests until deployments are assigned/)).toBeInTheDocument();
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: button }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/pools",
        body: {
          id: "anthropic",
          name: "Anthropic",
          source_kind: "anthropic",
          description: "",
          priority: 0,
          enabled,
          visibility: "restricted",
          default_discount: "1",
          bindings: [],
        },
      },
    ]),
  );
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
});

it("keeps an empty pool's configuration available when the server rejects its creation", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_input: string, options?: RequestInit) =>
      options?.method === "POST"
        ? new Response(JSON.stringify({ detail: "This resource pool ID already exists" }), { status: 409 })
        : new Response(JSON.stringify({ items: [] }), { status: 200 }),
    ),
  );
  const onClose = vi.fn();
  mount(
    <PoolWizard accessToken="test-admin" seed={{ name: "Anthropic", source_kind: "anthropic" }} onClose={onClose} />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Pool name" }), { target: { value: "My empty resource pool" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("button", { name: "Save and publish" }));
  expect(await screen.findByText("This resource pool ID already exists")).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByText("My empty resource pool")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByRole("textbox", { name: "Pool name" })).toHaveValue("My empty resource pool");
});

it("saves only explicit account pool-model pairs without creating cross-pool combinations", async () => {
  const writes = serveApi();
  mount(<PoolAccessEditor accessToken="test-admin" userId="customer-example" onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Custom pool-model pairs" }));
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox", { name: /^Allow alpha in Source A/ }));
  await user.click(screen.getByRole("checkbox", { name: /^Allow beta in Source B/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save account pool access" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/wallets/customer-example/pool-access",
        body: {
          mode: "custom",
          grants: [
            { pool_id: "pool-a", models: ["alpha"] },
            { pool_id: "pool-b", models: ["beta"] },
          ],
        },
      },
    ]),
  );
});

it("previews a fixed eligible route through the server without sending a provider request", async () => {
  const writes = serveApi();
  mount(<PoolPreview accessToken="test-admin" pool={poolA} onClose={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox", { name: "Customer account ID (optional)" }), {
    target: { value: "customer-example" },
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Public model" }));
  await user.click(await screen.findByRole("option", { name: "alpha" }));
  fireEvent.click(screen.getByRole("button", { name: "Preview route" }));
  expect(await screen.findByText("Eligible route found")).toBeInTheDocument();
  expect(writes).toEqual([
    {
      path: "/retail/pools/preview",
      body: { user_id: "customer-example", model: "alpha", policy: { mode: "fixed", pool_id: "pool-a" } },
    },
  ]);
});

it("does not activate routing or create pools by opening templates and review screens", async () => {
  const writes = serveApi();
  mount(<ResourcePoolsPanel />);
  expect(await screen.findByText("Source A")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Subscribe" }));
  expect(screen.getByRole("textbox", { name: "Pool name" })).toHaveValue("Anthropic Subscribe");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Review routing settings" }));
  expect(screen.getByText("unassigned-model")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable pool routing" })).toBeDisabled();
  expect(writes).toEqual([]);
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox", { name: /^I reviewed pool access, model coverage, and pricing/ }));
  fireEvent.click(screen.getByRole("button", { name: "Enable pool routing" }));
  await waitFor(() =>
    expect(writes).toEqual([{ path: "/retail/admin/pools/settings", body: { routing_enabled: true } }]),
  );
});
