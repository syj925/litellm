import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../../tests/test-utils";
import ChannelActivityPanel from "./ChannelActivityPanel";
import ChannelDetailsPanel from "./ChannelDetailsPanel";
import { channelDetailFixture, channelSummaryFixture } from "./testFixtures";

const auth = vi.hoisted(() => ({
  userRole: "Admin",
  accessToken: "fixture-admin",
  isAuthorized: true,
  isLoading: false,
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

beforeEach(() => {
  auth.userRole = "Admin";
  testQueryClient.clear();
});

afterEach(() => {
  cleanup();
  testQueryClient.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
});

describe("channel analytics", () => {
  it("shows channel rates and native USD with a real detail link and gateway-wide coverage", async () => {
    const fetch = vi.fn(async (_path: RequestInfo | URL) => new Response(JSON.stringify(channelSummaryFixture)));
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<ChannelActivityPanel />, { searchParams: { hours: "6" } });
    const link = await screen.findByRole("link", { name: "Primary fixture channel" });
    expect(link).toHaveAttribute("href", "/ui/channel-details?channel=channel-fixture&hours=6");
    expect(screen.getByText("$0.012345")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Gateway-wide attribution coverage" })).toHaveTextContent(
      "2 unattributed",
    );
    expect(screen.getByRole("region", { name: "Gateway-wide attribution coverage" })).toHaveTextContent(
      "model ID coverage 80%",
    );
    expect(fetch.mock.calls[0][0]).toContain("/retail/admin/channels/metrics?hours=6");
  });

  it("retains the last successful snapshot when a manual refresh fails", async () => {
    const user = userEvent.setup();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(channelSummaryFixture)))
      .mockResolvedValue(new Response(JSON.stringify({ detail: "Fixture refresh unavailable" }), { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<ChannelActivityPanel />);
    await screen.findByRole("link", { name: "Primary fixture channel" });
    await user.click(screen.getByRole("button", { name: "Refresh channel metrics" }));
    expect(await screen.findByText("Refresh failed; showing the last successful snapshot")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Primary fixture channel" })).toBeInTheDocument();
    expect(screen.getByText("$0.012345")).toBeInTheDocument();
  });

  it("keeps missing TTFT unknown and separates request links from upstream event IDs", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(channelDetailFixture))),
    );
    renderWithProviders(<ChannelDetailsPanel />, { searchParams: { channel: "channel-fixture" } });
    expect(await screen.findByRole("heading", { name: "Primary fixture channel" })).toBeInTheDocument();
    expect(screen.getByLabelText("TTFT p50")).toHaveTextContent("—");
    await user.click(screen.getByRole("tab", { name: "Models" }));
    expect(await screen.findByText("public-model-fixture")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Errors" }));
    const recent = await screen.findByRole("region", { name: "Recent channel errors" });
    expect(within(recent).getByRole("link", { name: "real-request-fixture" })).toHaveAttribute(
      "href",
      "/ui/logs?log_id=real-request-fixture",
    );
    expect(within(recent).queryByRole("link", { name: "upstream-event-fixture" })).not.toBeInTheDocument();
    expect(within(recent).getByRole("button", { name: "Copy upstream event ID" })).toBeInTheDocument();
  });

  it("does not request metrics or expose tables to ordinary accounts", () => {
    auth.userRole = "Internal User";
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<ChannelActivityPanel />);
    expect(screen.getByText("Administrator access required")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("polls visible pages, pauses in the background and respects the pause control", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn(async () => new Response(JSON.stringify(channelSummaryFixture)));
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<ChannelActivityPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    focusManager.setFocused(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    focusManager.setFocused(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    fireEvent.click(screen.getByRole("button", { name: "Pause refresh" }));
    const afterPause = fetch.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(fetch).toHaveBeenCalledTimes(afterPause);
    expect(screen.getByRole("button", { name: "Resume refresh" })).toBeInTheDocument();
  });
});
