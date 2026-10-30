import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import UserModelCatalog from "./UserModelCatalog";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ accessToken: "test-session", userId: "self" }),
}));
afterEach(() => vi.unstubAllGlobals());

const pool = {
  id: "pool-a",
  name: "Source A",
  source_kind: "bedrock",
  enabled: true,
  models: ["permitted-model", "not-granted-model"],
  allowed_models: ["permitted-model"],
};

function setup(body: object, status = 200) {
  const request = vi.fn().mockImplementation(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", request);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UserModelCatalog />
    </QueryClientProvider>,
  );
  return request;
}

it("shows only the account's live allowed model pairs through the customer endpoint", async () => {
  const request = setup({
    routing_enabled: false,
    items: [
      pool,
      { ...pool, id: "disabled", enabled: false, models: ["disabled-model"], allowed_models: ["disabled-model"] },
    ],
  });
  expect(await screen.findByText("permitted-model")).toBeInTheDocument();
  expect(screen.queryByText("not-granted-model")).not.toBeInTheDocument();
  expect(screen.queryByText("disabled-model")).not.toBeInTheDocument();
  expect(screen.getByText("AWS Bedrock")).toBeInTheDocument();
  expect(request.mock.calls.every(([url]) => url === "/retail/pools")).toBe(true);
});

it("filters available models without revealing additional ungranted candidates", async () => {
  setup({ routing_enabled: true, items: [pool] });
  await screen.findByText("permitted-model");
  fireEvent.change(screen.getByRole("textbox", { name: "Search available models" }), {
    target: { value: "not-granted" },
  });
  expect(screen.getByText("No models match your search.")).toBeInTheDocument();
  expect(screen.queryByText("not-granted-model")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Search available models" }), { target: { value: "Source A" } });
  expect(screen.getByText("permitted-model")).toBeInTheDocument();
});

it("fails closed when account access cannot be loaded", async () => {
  const request = setup({ detail: "Access verification unavailable" }, 403);
  expect(await screen.findByText("Unable to verify model access")).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(1);
});
