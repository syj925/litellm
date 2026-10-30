import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

describe("customer request cost presentation", () => {
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
