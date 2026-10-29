import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FormProvider, useForm } from "react-hook-form";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "../../../tests/test-utils";
import {
  MountedFormProvider,
  projectMountedValues,
  useMountRegistry,
  type MountedFormValues,
} from "../common_components/MountedFormField";
import type { ResourcePool } from "../retail/pools/types";
import type { NamedChannel } from "../retail/channels/types";
import ChannelSetup from "./ChannelSetup";

vi.mock("../networking", () => ({ getGlobalLitellmHeaderName: () => "Authorization", getProxyBaseUrl: () => "" }));

const pool: ResourcePool = {
  id: "pool-a",
  name: "Bedrock Claude",
  source_kind: "bedrock",
  description: "",
  enabled: false,
  visibility: "restricted",
  priority: 100,
  default_discount: "1",
  version: 1,
  bindings: [],
  models: [],
};
const channel: NamedChannel = {
  id: "channel-a",
  name: "Supplier A",
  pool_id: pool.id,
  pool_name: pool.name,
  deployment_ids: ["deployment-a"],
  models: ["shared"],
  deployment_count: 1,
  status: "active",
  version: 1,
};

function serve(status = 200) {
  const writes: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("channels"))
        return new Response(
          JSON.stringify(status === 200 ? { enabled: true, items: [channel] } : { detail: "Unavailable" }),
          { status },
        );
      if (options?.method === "POST") {
        const body: ResourcePool = JSON.parse(String(options.body));
        writes.push({ path, body });
        return new Response(JSON.stringify({ ...body, version: 1, models: [], bindings: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ items: [pool] }), { status: 200 });
    }),
  );
  return writes;
}

function Harness({ onSubmit }: { onSubmit: (values: MountedFormValues) => void }) {
  const form = useForm<MountedFormValues>();
  const registry = useMountRegistry();
  return (
    <FormProvider {...form}>
      <MountedFormProvider value={{ control: form.control, registry }}>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (await form.trigger()) onSubmit(projectMountedValues(registry, form.getValues));
          }}
        >
          <ChannelSetup accessToken="test-admin" />
          <button type="submit">Save</button>
        </form>
      </MountedFormProvider>
    </FormProvider>
  );
}

function mount(onSubmit = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness onSubmit={onSubmit} />
    </QueryClientProvider>,
  );
  return onSubmit;
}
afterEach(() => vi.unstubAllGlobals());

it("passes the channel name and selected pool together to the model form", async () => {
  serve();
  const submit = mount();
  await userEvent.type(await screen.findByLabelText("Channel name"), "Supplier B");
  await userEvent.click(screen.getByRole("combobox", { name: "Resource pool" }));
  await userEvent.click(await screen.findByRole("option", { name: "Bedrock Claude · Draft" }));
  expect(screen.getByText(/Draft pool: publish it after reviewing/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
  await waitFor(() =>
    expect(submit).toHaveBeenCalledExactlyOnceWith({
      retail_channel: { mode: "new", name: "Supplier B", pool_id: "pool-a" },
    }),
  );
});

it("locks an existing channel to its own named pool when appending models", async () => {
  serve();
  const submit = mount();
  await screen.findByLabelText("Channel name");
  await userEvent.click(screen.getByRole("radio", { name: "Add models to existing channel" }));
  await userEvent.click(screen.getByRole("combobox", { name: "Existing channel" }));
  await userEvent.click(await screen.findByRole("option", { name: "Supplier A · Bedrock Claude · 1 deployments" }));
  expect(screen.getByText(/Resource pool: Bedrock Claude/)).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Resource pool" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
  await waitFor(() =>
    expect(submit).toHaveBeenCalledExactlyOnceWith({ retail_channel: { mode: "existing", channel_id: "channel-a" } }),
  );
});

it("creates a restricted draft pool inline and selects it without publishing", async () => {
  const writes = serve();
  mount();
  await userEvent.click(await screen.findByRole("button", { name: "Create pool" }));
  fireEvent.change(screen.getByLabelText("Pool name"), { target: { value: "New source" } });
  await userEvent.click(screen.getByRole("button", { name: "Create draft" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({
    path: "/retail/admin/pools",
    body: {
      id: expect.any(String),
      name: "New source",
      source_kind: "custom",
      description: "",
      enabled: false,
      visibility: "restricted",
      priority: 100,
      default_discount: "1",
      bindings: [],
    },
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

it("does not fall back to native creation on a catalog authorization failure", async () => {
  serve(403);
  const submit = mount();
  await screen.findByText("Unable to load channel configuration");
  await userEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
  expect(submit).not.toHaveBeenCalled();
  expect(await screen.findByText("Channel configuration is unavailable. Retry before saving")).toBeInTheDocument();
});

it("removes the extension fields when the retail routes are not installed", async () => {
  serve(404);
  const submit = mount();
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "Channel and resource pool" })).not.toBeInTheDocument(),
  );
  await userEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
  await waitFor(() => expect(submit).toHaveBeenCalledExactlyOnceWith({}));
});
