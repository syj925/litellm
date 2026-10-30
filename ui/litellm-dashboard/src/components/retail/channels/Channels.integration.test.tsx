import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import ChannelEditor from "./ChannelEditor";
import ChannelsPanel from "./ChannelsPanel";
import DeploymentChannelIdentity, { ChannelIdentityProvider } from "./DeploymentChannelIdentity";
import type { ResourcePool } from "../pools/types";
import type { NamedChannel } from "./types";
import { chooseSelectOption } from "../../../../tests/test-utils";

const auth = vi.hoisted(() => ({ accessToken: "test-admin", userRole: "Admin", isAuthorized: true, isLoading: false }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));

const pool: ResourcePool = {
  id: "pool-a",
  name: "Source A",
  source_kind: "anthropic",
  description: "",
  enabled: true,
  visibility: "restricted",
  priority: 100,
  default_discount: "1",
  version: 1,
  bindings: [],
  models: ["shared"],
};
const channel: NamedChannel = {
  id: "channel-a",
  name: "Claude primary",
  pool_id: pool.id,
  pool_name: pool.name,
  deployment_ids: ["a", "b"],
  models: ["shared", "other"],
  deployment_count: 2,
  status: "active",
  version: 7,
};
const connection = {
  channel_id: channel.id,
  version: channel.version,
  deployment_count: 2,
  editable_deployment_count: 2,
  can_edit: true,
  reason: null,
  api_base: "https://one.example.test/v1",
  api_base_mixed: false,
  has_api_key: true,
  credential_mode: "direct",
};
const deployments = [
  {
    deployment_id: "a",
    model: "shared",
    provider: "anthropic",
    credential_name: null,
    connection_id: "connection-a",
    endpoint_host: "one.example.test",
  },
  {
    deployment_id: "b",
    model: "other",
    provider: "anthropic",
    credential_name: null,
    connection_id: "connection-a",
    endpoint_host: "one.example.test",
  },
];

function mount(element: React.ReactElement) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}
    >
      {element}
    </QueryClientProvider>,
  );
}
afterEach(() => {
  auth.userRole = "Admin";
  vi.unstubAllGlobals();
});

it("names an existing connection and assigns all its models to a single pool", async () => {
  const writes: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_path, options: RequestInit) => {
      writes.push(JSON.parse(String(options.body)));
      return new Response(JSON.stringify(channel), { status: 200 });
    }),
  );
  const close = vi.fn();
  mount(<ChannelEditor deployments={deployments} pools={[pool]} accessToken="test-admin" onClose={close} />);
  fireEvent.change(screen.getByRole("textbox", { name: "Channel name" }), { target: { value: channel.name } });
  const user = userEvent.setup();
  await chooseSelectOption(user, screen.getByRole("combobox", { name: "Resource pool" }), pool.name);
  await user.click(screen.getByRole("checkbox", { name: /Select connection one.example.test/ }));
  expect(screen.getByText("2 deployments selected")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save channel" }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(writes).toEqual([{ name: channel.name, pool_id: pool.id, deployment_ids: ["a", "b"] }]);
});

it("sends the channel revision and keeps a rejected stale edit open", async () => {
  const fetchMock = vi.fn(async (path: string, _options?: RequestInit) =>
    path.endsWith("/connection")
      ? new Response(JSON.stringify(connection))
      : new Response(JSON.stringify({ detail: "Channel changed. Reload before saving." }), { status: 409 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const close = vi.fn();
  mount(
    <ChannelEditor
      channel={channel}
      deployments={deployments}
      pools={[pool]}
      accessToken="test-admin"
      onClose={close}
    />,
  );
  await userEvent.setup().click(screen.getByRole("button", { name: "Save channel" }));
  expect(await screen.findByText("Channel changed. Reload before saving.")).toBeInTheDocument();
  const write = fetchMock.mock.calls.find(([, options]) => options?.method === "PUT");
  const expectedWrite = {
    name: channel.name,
    pool_id: pool.id,
    deployment_ids: ["a", "b"],
    expected_version: 7,
  };
  expect(JSON.parse(String(write?.[1]?.body))).toEqual(expectedWrite);
  expect(close).not.toHaveBeenCalled();
});

it("distinguishes two same-name model deployments by their channel and pool", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            enabled: true,
            items: [
              channel,
              { ...channel, id: "channel-b", name: "Claude secondary", pool_name: "Source B", deployment_ids: ["c"] },
            ],
          }),
        ),
    ),
  );
  mount(
    <>
      <ChannelIdentityProvider>
        <DeploymentChannelIdentity deploymentId="a" />
        <DeploymentChannelIdentity deploymentId="c" />
      </ChannelIdentityProvider>
    </>,
  );
  expect(await screen.findByRole("link", { name: "Claude primary · Source A" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Claude secondary · Source B" })).toBeInTheDocument();
});

it("does not load or expose upstream channels to ordinary users", () => {
  auth.userRole = "Internal User";
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  mount(<ChannelsPanel />);
  expect(screen.getByText("Administrator access required")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("opens activity for the exact channel rather than its shared public model", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      if (path.endsWith("/admin/channels")) return new Response(JSON.stringify({ enabled: true, items: [channel] }));
      if (path.endsWith("/admin/pools/deployments")) return new Response(JSON.stringify({ items: deployments }));
      return new Response(JSON.stringify({ items: [pool] }));
    }),
  );
  mount(<ChannelsPanel />);
  const name = await screen.findByRole("link", { name: channel.name });
  expect(name).toHaveAttribute("href", "/ui/channel-details?channel=channel-a");
  expect(screen.getByRole("link", { name: "View activity" })).toHaveAttribute("href", name.getAttribute("href"));
  expect(screen.getByRole("link", { name: "Channel activity" })).toHaveAttribute(
    "href",
    "/ui/usage?usage_tab=channels",
  );
});

it("lets an operator remove a deleted deployment instead of trapping an unsavable channel", async () => {
  const writes: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/connection")) return new Response(JSON.stringify(connection));
      writes.push(JSON.parse(String(options?.body)));
      return new Response(JSON.stringify(channel));
    }),
  );
  const close = vi.fn();
  mount(
    <ChannelEditor
      channel={{ ...channel, deployment_ids: ["a", "deleted"] }}
      deployments={deployments}
      pools={[pool]}
      accessToken="test-admin"
      onClose={close}
    />,
  );
  expect(screen.getByRole("button", { name: "Save channel" })).toBeDisabled();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Remove missing deployment deleted" }));
  await user.click(screen.getByRole("button", { name: "Save channel" }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(writes).toEqual([{ name: channel.name, pool_id: pool.id, deployment_ids: ["a"], expected_version: 7 }]);
});

it("reviews the pending batch before requesting channel recovery", async () => {
  const writes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (options?.method === "POST") {
        writes.push(input);
        return new Response(JSON.stringify({ ...channel, pending_deployment_ids: [] }));
      }
      if (input.endsWith("/admin/channels"))
        return new Response(
          JSON.stringify({
            enabled: true,
            items: [{ ...channel, status: "error", pending_deployment_ids: ["incomplete"] }],
          }),
        );
      if (input.endsWith("/admin/pools/deployments")) return new Response(JSON.stringify({ items: deployments }));
      return new Response(JSON.stringify({ items: [pool] }));
    }),
  );
  mount(<ChannelsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Review recovery" }));
  expect(screen.getByText("1 pending deployments")).toBeInTheDocument();
  expect(writes).toEqual([]);
  await user.click(screen.getByRole("button", { name: "Recover incomplete batch" }));
  await waitFor(() => expect(writes).toEqual(["/retail/admin/channels/channel-a/recover"]));
});
