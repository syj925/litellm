import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import OrganizationOidcLogin, { OrganizationOidcError } from "./OrganizationOidcLogin";

const gateway = vi.hoisted(() => ({ base: "https://gateway.example" }));
vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => gateway.base }));

const originalLocation = window.location;
const navigate = vi.fn();
const clients: QueryClient[] = [];
const ready = { enabled: true, available: true, label: "Continue with organization account" };

function location(search = "") {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin: "https://gateway.example", search, pathname: "/ui/login/", assign: navigate },
  });
}

function renderEntry(supported = true, disabled = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const tree = (
    <QueryClientProvider client={client}>
      <OrganizationOidcLogin supported={supported} disabled={disabled} />
    </QueryClientProvider>
  );
  return { ...render(tree), client };
}

function serve(body: unknown = ready, status = 200) {
  const state = { body, status };
  const requests: { url: string; options: RequestInit | undefined }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      requests.push({ url: input, options });
      return new Response(JSON.stringify(state.body), { status: state.status });
    }),
  );
  return { state, requests };
}

beforeEach(() => {
  gateway.base = "https://gateway.example";
  navigate.mockReset();
  location();
  document.cookie = "litellm_return_url=; Max-Age=0; path=/";
});

afterEach(() => {
  clients.forEach((client) => client.clear());
  clients.length = 0;
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

it.each([
  ["https://gateway.example/ui/models/?search=public%20model", "/ui/models/?search=public%20model"],
  ["https://other.example/ui/models", "/ui/"],
])("starts a top-level server flow with a safe return target for %s", async (candidate, expected) => {
  location(`?redirect_to=${encodeURIComponent(candidate)}`);
  const { requests } = serve();
  renderEntry();
  await userEvent.click(await screen.findByRole("button", { name: ready.label }));
  expect(navigate).toHaveBeenCalledTimes(1);
  const destination = new URL(navigate.mock.calls[0][0] as string);
  expect(destination.origin).toBe("https://gateway.example");
  expect(destination.pathname).toBe("/retail/auth/oidc/start");
  expect(Array.from(destination.searchParams.keys())).toEqual(["return_to"]);
  expect(destination.searchParams.get("return_to")).toBe(expected);
  expect(requests).toHaveLength(1);
  const request = requests[0];
  expect(request.url).toBe("https://gateway.example/retail/auth/oidc/status");
  const expectedOptions = {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  };
  expect(request.options).toMatchObject(expectedOptions);
  expect(new Headers(request.options?.headers).has("Authorization")).toBe(false);
  expect(request.options?.body).toBeUndefined();
  expect(screen.getByText("Console access. Use Virtual Keys for model API calls.")).toBeInTheDocument();
});

it.each([
  [{ ...ready, enabled: false }, 200],
  [{ ...ready, available: "true" }, 200],
  [{ ...ready, issuer: "private-issuer" }, 200],
  [{ detail: "private-provider-failure" }, 404],
  [{ detail: "private-provider-failure" }, 503],
])("hides unavailable or invalid optional discovery without showing server details: %j", async (body, status) => {
  const { requests } = serve(body, status);
  const { client } = renderEntry();
  await waitFor(() => expect(requests).toHaveLength(1));
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.queryByText(/private-provider|private-issuer/)).not.toBeInTheDocument();
});

it("silently contains a discovery network failure", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("private-provider-network-failure");
    }),
  );
  const { client } = renderEntry();
  await waitFor(() =>
    expect(client.getQueryState(["retail", "auth", "oidc", "status", gateway.base])?.status).toBe("error"),
  );
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.queryByText(/private-provider/)).not.toBeInTheDocument();
});

it("shows a nonblocking recovery hint only for a configured but unavailable provider", async () => {
  serve({ ...ready, available: false });
  renderEntry();
  expect(await screen.findByRole("status")).toHaveTextContent("You can use your username and password");
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("hides old ready status after a failed refresh and restores the entry only after a successful check", async () => {
  const { state } = serve();
  const { client } = renderEntry();
  await screen.findByRole("button", { name: ready.label });
  state.status = 503;
  state.body = { detail: "private-provider-failure" };
  await act(async () => client.invalidateQueries({ queryKey: ["retail", "auth", "oidc", "status"] }));
  await waitFor(() => expect(screen.queryByRole("button")).not.toBeInTheDocument());
  state.status = 200;
  state.body = ready;
  await act(async () => client.invalidateQueries({ queryKey: ["retail", "auth", "oidc", "status"] }));
  expect(await screen.findByRole("button", { name: ready.label })).toBeEnabled();
});

it("waits for fresh discovery on remount even when a ready response is cached", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
  const client = new QueryClient();
  clients.push(client);
  client.setQueryData(["retail", "auth", "oidc", "status", gateway.base], ready);
  render(
    <QueryClientProvider client={client}>
      <OrganizationOidcLogin supported disabled={false} />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it.each([false, true])(
  "does not discover or navigate across unsupported worker boundaries, supported=%s",
  async (supported) => {
    gateway.base = supported ? "https://worker.example" : "https://gateway.example";
    const { requests } = serve();
    renderEntry(supported);
    await act(async () => undefined);
    expect(requests).toHaveLength(0);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  },
);

it("keeps the optional entry disabled while a password submission is in progress", async () => {
  serve();
  renderEntry(true, true);
  const button = await screen.findByRole("button", { name: ready.label });
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(navigate).not.toHaveBeenCalled();
});

it("renders the public provider label as text and a fixed safe failure message", async () => {
  location("?oidc_error=failed&error_description=private-provider-secret");
  serve({ ...ready, label: "<script>organization label</script>" });
  renderEntry();
  render(<OrganizationOidcError />);
  expect(await screen.findByRole("button", { name: "<script>organization label</script>" })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Try again or use your username and password");
  expect(screen.queryByText("private-provider-secret")).not.toBeInTheDocument();
});
