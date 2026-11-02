import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LogDetailContent } from "./LogDetailContent";
import type { LogEntry } from "../types";

const session = vi.hoisted(() => ({ role: "Internal User" }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => ({ userRole: session.role }) }));

const log = {
  request_id: "customer-request",
  api_key: "customer-key-id",
  team_id: "",
  model: "public-model",
  model_id: "",
  call_type: "completion",
  spend: null,
  total_tokens: 12,
  prompt_tokens: 8,
  completion_tokens: 4,
  startTime: "2026-10-30T00:00:00Z",
  endTime: "2026-10-30T00:00:01Z",
  cache_hit: "false",
  metadata: { status: "success" },
  messages: [{ role: "user", content: "hello" }],
  response: { choices: [{ message: { role: "assistant", content: "welcome" } }] },
} as unknown as LogEntry;

beforeEach(() => {
  session.role = "Internal User";
});
afterEach(() => vi.unstubAllGlobals());

describe("customer request cost presentation", () => {
  it("removes an open administrator JSON view when the session role becomes a customer", () => {
    session.role = "Admin";
    const privateLog = {
      ...log,
      proxy_server_request: { messages: [{ role: "user", content: "private-rewritten-request" }] },
    };
    const { rerender } = render(<LogDetailContent logEntry={privateLog} />);
    fireEvent.click(screen.getByRole("tab", { name: "JSON" }));
    session.role = "internal_user";
    rerender(<LogDetailContent logEntry={privateLog} />);
    expect(screen.getByText("hello")).toBeVisible();
    expect(screen.queryByRole("tab", { name: "JSON" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy JSON" })).not.toBeInTheDocument();
    expect(screen.queryByText(/private-rewritten-request/)).not.toBeInTheDocument();
  });

  it.each(["internal_user", "internal_user_viewer", "customer", "team_admin", "org_admin", "unknown"])(
    "keeps private upstream diagnostics out of %s logs while retaining their messages and IP",
    (role) => {
      session.role = role;
      const privateLog: LogEntry = {
        ...log,
        custom_llm_provider: "private-provider",
        model_id: "private-deployment",
        api_base: "https://private-upstream.invalid",
        requester_ip_address: "203.0.113.9",
        cache_key: "private-cache-hash",
        proxy_server_request: { messages: [{ role: "user", content: "private-rewritten-request" }] },
        metadata: {
          status: "success",
          user_api_key: "private-key-hash",
          retail_resource_pool_name: "private-pool",
          retail_resource_source_kind: "private-source",
          provider_cost: 1.25,
          error_information: { error_message: "private-stack" },
        },
      };
      render(<LogDetailContent logEntry={privateLog} />);
      expect(screen.getByText("hello")).toBeInTheDocument();
      expect(screen.getByText("welcome")).toBeInTheDocument();
      expect(screen.getByText("203.0.113.9")).toBeInTheDocument();
      expect(screen.queryByText(/private-/)).not.toBeInTheDocument();
      expect(screen.queryByText("API integration")).not.toBeInTheDocument();
      expect(screen.queryByText("Deployment ID")).not.toBeInTheDocument();
      expect(screen.queryByText("Metadata")).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: "JSON" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Copy Metadata" })).not.toBeInTheDocument();
    },
  );

  it("replaces raw upstream errors with a safe failure message for customers", () => {
    render(
      <LogDetailContent
        logEntry={{
          ...log,
          status: "failure",
          response: { error: { message: "private-response-stack" } },
          metadata: {
            status: "failure",
            error_information: { error_message: "private-provider-host", error_class: "private-provider-class" },
          },
        }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("The request could not be completed.");
    expect(screen.queryByText(/private-/)).not.toBeInTheDocument();
  });

  it.each(["Internal User", "proxy_admin_viewer"])(
    "does not request sensitive administrator traces for %s even with an access token",
    (role) => {
      session.role = role;
      const fetcher = vi.fn();
      vi.stubGlobal("fetch", fetcher);
      render(<LogDetailContent logEntry={log} accessToken="customer-token" />);
      expect(screen.queryByRole("region", { name: "Administrator request trace" })).not.toBeInTheDocument();
      expect(fetcher).not.toHaveBeenCalled();
    },
  );
  it.each(["Internal User", "Org Admin"])("keeps %s logs free of native and fabricated zero charges", (role) => {
    session.role = role;
    render(<LogDetailContent logEntry={log} />);
    expect(screen.getByRole("link", { name: "Billing" })).toHaveAttribute("href", expect.stringContaining("/billing"));
    expect(screen.queryByText("Cost Breakdown")).not.toBeInTheDocument();
    expect(screen.queryByText("Cost")).not.toBeInTheDocument();
    expect(screen.queryByText(/Cost: \$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("welcome")).toBeInTheDocument();
  });

  it.each(["Admin", "proxy_admin_viewer"])("retains native cost details for %s", (role) => {
    session.role = role;
    render(<LogDetailContent logEntry={{ ...log, spend: 1.25 }} />);
    expect(screen.getByText("Cost Breakdown")).toBeInTheDocument();
    expect(screen.getByText("Cost")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Billing" })).not.toBeInTheDocument();
  });
});
