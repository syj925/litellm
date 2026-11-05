import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RequestPoliciesPanel from "./RequestPoliciesPanel";
import type { PolicyBinding, PromptDetails, RequestPolicy } from "./types";

const auth = vi.hoisted(() => ({
  userRole: "proxy_admin",
  isViewOnly: false,
  accessToken: "test-session",
  userId: "admin-one",
  isAuthorized: true,
  isLoading: false,
}));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => ({ ...auth }) }));
const base = "/retail/admin/request-policies";
const common = { enabled: true, version: 3, created_at: "2026-11-05T00:00:00Z", updated_at: "2026-11-05T00:00:00Z" };
const policy: RequestPolicy = {
  ...common,
  id: "policy-one",
  name: "Safe output",
  prompt_id: null,
  definition: { allowed_routes: null, max_output_tokens: 100, temperature: null, top_p: null, seed: null },
};
const prompt: PromptDetails = {
  ...common,
  id: "prompt-one",
  name: "Private instructions",
  content: "PRIVATE PROMPT CONTENT",
};
const binding: PolicyBinding = {
  ...common,
  id: "binding-one",
  target_id: "opaque-target",
  policy_id: policy.id,
  target_kind: "key",
  target_label: "Application key",
  policy_name: policy.name,
};
interface Call {
  url: URL;
  method: string;
  body: Record<string, unknown> | null;
  headers: Headers;
  signal: AbortSignal | null | undefined;
}
function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
function page<T>(items: T[], total = items.length) {
  return { items, total, limit: 25, offset: 0 };
}
function serve(handler?: (call: Call) => Response | Promise<Response>, enabled = true) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const call = {
        url: new URL(input),
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as Record<string, unknown>) : null,
        headers: new Headers(options?.headers),
        signal: options?.signal,
      };
      calls.push(call);
      if (call.url.pathname === "/retail/config") return json({ request_policies_enabled: enabled });
      if (handler) return handler(call);
      if (call.url.pathname === `${base}/policies`) return json(page([policy]));
      if (call.url.pathname === `${base}/prompts`) return json(page([{ ...common, id: prompt.id, name: prompt.name }]));
      if (call.url.pathname === `${base}/bindings`) return json(page([binding]));
      if (call.url.pathname === `${base}/prompts/${prompt.id}`) return json(prompt);
      return json({ detail: "Unexpected request" }, 500);
    }),
  );
  return calls;
}
beforeEach(() => {
  const freshAuth = {
    userRole: "proxy_admin",
    isViewOnly: false,
    accessToken: "test-session",
    userId: "admin-one",
    isAuthorized: true,
    isLoading: false,
  };
  Object.assign(auth, freshAuth);
  testQueryClient.clear();
});
afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

it.each(["Internal User", "proxy_admin_viewer", "org_admin"])(
  "does not fetch private data or config for %s",
  async (role) => {
    auth.userRole = role;
    const calls = serve();
    renderWithProviders(<RequestPoliciesPanel />);
    expect(screen.getByText("Gateway administrator access required")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  },
);
it("does not fetch policies when the gateway flag is disabled", async () => {
  const calls = serve(undefined, false);
  renderWithProviders(<RequestPoliciesPanel />);
  expect(await screen.findByText("Request policies is not enabled")).toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config"]);
});
it("shows a recoverable loading error without exposing stale policy rows", async () => {
  serve(() => json({ detail: "Authority unavailable" }, 503));
  renderWithProviders(<RequestPoliciesPanel />);
  expect(await screen.findByText("Unable to load policies")).toBeInTheDocument();
  expect(screen.getByText("Authority unavailable")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create policy" })).toBeDisabled();
});
it("reads prompt metadata only until an explicit edit and clears it on cancel", async () => {
  const calls = serve();
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  const user = userEvent.setup();
  const edit = await screen.findByRole("button", { name: "Edit Private instructions" });
  expect(calls.some((call) => call.url.pathname === `${base}/prompts/${prompt.id}`)).toBe(false);
  expect(screen.queryByDisplayValue(prompt.content)).not.toBeInTheDocument();
  await user.click(edit);
  expect(await screen.findByLabelText("Prompt content")).toHaveValue(prompt.content);
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByDisplayValue(prompt.content)).not.toBeInTheDocument();
  expect(
    testQueryClient
      .getQueryCache()
      .getAll()
      .some((query) => JSON.stringify(query.state.data).includes(prompt.content)),
  ).toBe(false);
});
it("clears an open private prompt editor immediately when the current account loses admin privileges", async () => {
  serve();
  const view = renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  await userEvent.setup().click(await screen.findByRole("button", { name: "Edit Private instructions" }));
  expect(await screen.findByDisplayValue(prompt.content)).toBeInTheDocument();
  auth.userRole = "Internal User";
  view.rerender(<RequestPoliciesPanel />);
  expect(screen.getByText("Gateway administrator access required")).toBeInTheDocument();
  expect(screen.queryByDisplayValue(prompt.content)).not.toBeInTheDocument();
});
it("revokes all private editor state on a server-side 403 and starts no more private queries", async () => {
  const calls = serve((call) =>
    call.url.pathname.endsWith(`/prompts/${prompt.id}`)
      ? json({ detail: "Revoked" }, 403)
      : json(page([{ ...common, id: prompt.id, name: prompt.name }])),
  );
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  await userEvent.setup().click(await screen.findByRole("button", { name: "Edit Private instructions" }));
  expect(await screen.findByText("Administrator access is no longer available")).toBeInTheDocument();
  expect(screen.queryByText(prompt.name)).not.toBeInTheDocument();
  expect(calls.filter((call) => call.url.pathname.startsWith(base))).toHaveLength(2);
});
it("submits explicit null clearing and zero-valued parameters with optimistic version and idempotency", async () => {
  const calls = serve((call) => {
    if (call.method === "PUT") return json(policy);
    return json(page(call.url.pathname.endsWith("/prompts") ? [] : [policy]));
  });
  renderWithProviders(<RequestPoliciesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Safe output" }));
  fireEvent.change(screen.getByLabelText("Output cap"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("Temperature"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "-25" } });
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1));
  const write = calls.find((call) => call.method === "PUT");
  const expected = {
    name: policy.name,
    enabled: true,
    prompt_id: null,
    expected_version: 3,
    definition: { allowed_routes: null, max_output_tokens: null, temperature: 0, top_p: null, seed: -25 },
  };
  expect(write?.body).toEqual(expected);
  expect(write?.headers.get("Idempotency-Key")).toMatch(/^[\da-f-]{36}$/);
});
it("blocks conflict retries until the administrator closes and refreshes", async () => {
  const calls = serve((call) => {
    if (call.method === "PUT") return json({ detail: "Version conflict" }, 409);
    return json(page(call.url.pathname.endsWith("/prompts") ? [] : [policy]));
  });
  renderWithProviders(<RequestPoliciesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Safe output" }));
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  expect(await screen.findByText("Changes need review")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});
it("prevents duplicate creates while the first write is unresolved", async () => {
  let finish: ((response: Response) => void) | undefined;
  const calls = serve((call) =>
    call.method === "POST"
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : json(page([])),
  );
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create prompt" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New prompt" } });
  fireEvent.change(screen.getByLabelText("Prompt content"), { target: { value: "Be concise." } });
  await user.click(screen.getByRole("button", { name: "Save prompt" }));
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Create prompt" })).toBeDisabled();
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
  finish?.(json(prompt));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Create saved prompt" })).not.toBeInTheDocument());
  expect(await screen.findByText("No prompts yet. Create a prompt to get started.")).toBeInTheDocument();
});
it("searches and pages target labels on the server then binds only the opaque selected reference", async () => {
  const calls = serve((call) => {
    if (call.method === "POST") return json(binding);
    if (call.url.pathname.endsWith("/targets"))
      return json(page([{ id: "opaque-selected", kind: "key", label: "Checkout client" }], 60));
    return json(page(call.url.pathname.endsWith("/policies") ? [policy] : []));
  });
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "bindings" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create binding" }));
  fireEvent.change(await screen.findByLabelText("Target type"), { target: { value: "key" } });
  fireEvent.change(screen.getByLabelText("Search targets"), { target: { value: "Checkout" } });
  await user.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(calls.some((call) => call.url.searchParams.get("q") === "Checkout")).toBe(true));
  await user.click(
    within(screen.getByRole("form", { name: "Create policy binding" })).getByRole("button", { name: "Next page" }),
  );
  await waitFor(() =>
    expect(
      calls.some((call) => call.url.pathname.endsWith("/targets") && call.url.searchParams.get("offset") === "25"),
    ).toBe(true),
  );
  await user.click(
    within(screen.getByRole("group", { name: "Available targets" })).getByRole("radio", { name: "Checkout client" }),
  );
  fireEvent.change(screen.getByLabelText("Request policy"), { target: { value: policy.id } });
  await user.click(
    within(screen.getByRole("form", { name: "Create policy binding" })).getByRole("button", { name: "Save binding" }),
  );
  await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
  expect(calls.find((call) => call.method === "POST")?.body).toEqual({
    target_id: "opaque-selected",
    policy_id: policy.id,
    enabled: true,
  });
});
it("does not fetch for a read-only admin session and discards an unfinished prompt request on unmount", async () => {
  auth.isViewOnly = true;
  const calls = serve();
  const blocked = renderWithProviders(<RequestPoliciesPanel />);
  expect(calls).toHaveLength(0);
  blocked.unmount();
  auth.isViewOnly = false;
  const pendingCalls = serve((call) =>
    call.url.pathname.endsWith(`/prompts/${prompt.id}`)
      ? new Promise<Response>(() => {})
      : json(page([{ ...common, id: prompt.id, name: prompt.name }])),
  );
  const view = renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  await userEvent.setup().click(await screen.findByRole("button", { name: "Edit Private instructions" }));
  expect(await screen.findByText("Loading prompt content")).toBeInTheDocument();
  view.unmount();
  expect(pendingCalls.find((call) => call.url.pathname.endsWith(`/prompts/${prompt.id}`))?.signal?.aborted).toBe(true);
});
it("saves a paused policy with an explicit deny-all route restriction and an attached saved prompt", async () => {
  const calls = serve((call) => {
    if (call.method === "POST") return json(policy);
    return json(page(call.url.pathname.endsWith("/prompts") ? [{ ...common, id: prompt.id, name: prompt.name }] : []));
  });
  renderWithProviders(<RequestPoliciesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create policy" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Paused guard" } });
  await user.click(screen.getByRole("checkbox", { name: "Restrict API routes" }));
  for (const route of ["chat.completions", "responses", "messages"])
    await user.click(screen.getByRole("checkbox", { name: route }));
  await user.click(screen.getByRole("checkbox", { name: "Policy enabled" }));
  expect(await screen.findByRole("option", { name: prompt.name })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Saved prompt"), { target: { value: prompt.id } });
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
  expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({
    enabled: false,
    prompt_id: prompt.id,
    definition: { allowed_routes: [] },
  });
  expect(calls.some((call) => call.url.pathname.endsWith(`/prompts/${prompt.id}`))).toBe(false);
});
it("stops applying a binding using its immutable target and current version", async () => {
  const calls = serve((call) => {
    if (call.method === "PUT") return json({ ...binding, enabled: false });
    return json(page(call.url.pathname.endsWith("/policies") ? [policy] : [binding]));
  });
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "bindings" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit Application key" }));
  await user.click(screen.getByRole("checkbox", { name: "Apply policy" }));
  await user.click(screen.getByRole("button", { name: "Save binding" }));
  await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
  expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
    policy_id: policy.id,
    enabled: false,
    expected_version: 3,
  });
  expect(calls.some((call) => call.url.pathname.endsWith("/targets"))).toBe(false);
});
it("reuses the idempotency key for an explicit retry after an uncertain write failure", async () => {
  const calls = serve((call) =>
    call.method === "POST" ? json({ detail: "Temporarily unavailable" }, 503) : json(page([])),
  );
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create prompt" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Retry prompt" } });
  fireEvent.change(screen.getByLabelText("Prompt content"), { target: { value: "Private instructions" } });
  await user.click(screen.getByRole("button", { name: "Save prompt" }));
  expect(await screen.findByText("Temporarily unavailable")).toBeInTheDocument();
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Save prompt" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(2));
  const writes = calls.filter((call) => call.method === "POST");
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
});
it("bounds non-ASCII prompt content by UTF-8 bytes before sending a write", async () => {
  const calls = serve(() => json(page([])));
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "prompts" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create prompt" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Multilingual prompt" } });
  fireEvent.change(screen.getByLabelText("Prompt content"), { target: { value: "界".repeat(2667) } });
  expect(
    screen.getByText("8,001 / 8,000 UTF-8 bytes. Content is private to gateway administrators."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save prompt" })).toBeDisabled();
  expect(calls.some((call) => call.method === "POST")).toBe(false);
  fireEvent.change(screen.getByLabelText("Prompt content"), { target: { value: "界".repeat(2666) + "ab" } });
  expect(screen.getByRole("button", { name: "Save prompt" })).toBeEnabled();
});
it("removes a binding using the shown version only after explicit confirmation", async () => {
  const calls = serve((call) => (call.method === "DELETE" ? json({ deleted: true }) : json(page([binding]))));
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { tab: "bindings" } });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Delete Application key" }));
  expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Delete permanently" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(1));
  expect(calls.find((call) => call.method === "DELETE")?.url.searchParams.get("version")).toBe("3");
});
it("keeps tab and pagination URL state while preserving unrelated parameters", async () => {
  const calls = serve((call) =>
    json(
      page(
        [{ ...policy, name: call.url.searchParams.get("offset") === "25" ? "Second page policy" : policy.name }],
        51,
      ),
    ),
  );
  const onUrlUpdate = vi.fn();
  renderWithProviders(<RequestPoliciesPanel />, { searchParams: { retained: "yes" }, onUrlUpdate });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Next page" }));
  expect(await screen.findByText("Second page policy")).toBeInTheDocument();
  expect(calls.at(-1)?.url.searchParams.get("offset")).toBe("25");
  expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get("retained")).toBe("yes");
});
