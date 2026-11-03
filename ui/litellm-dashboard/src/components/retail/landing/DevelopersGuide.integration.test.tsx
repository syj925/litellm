import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setServerRootPath } from "@/lib/serverRootPath";
import DevelopersGuide from "./DevelopersGuide";

const state = vi.hoisted(() => ({ authLoading: false, proxyBaseUrl: "https://gateway.example/team-x" }));

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authLoading: state.authLoading }) }));
vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => state.proxyBaseUrl }));
vi.mock("next/font/local", () => ({ default: () => ({ variable: "guide-font" }) }));

beforeEach(() => {
  vi.stubEnv("NODE_ENV", "production");
  setServerRootPath("/team-x");
  state.authLoading = false;
  state.proxyBaseUrl = "https://gateway.example/team-x";
});

afterEach(() => {
  setServerRootPath("/");
  vi.unstubAllEnvs();
});

describe("public developer guide", () => {
  it("stays readable anonymously and connects setup and navigation to deployment-prefixed pages", () => {
    state.authLoading = true;
    setServerRootPath("/");
    const { rerender } = render(<DevelopersGuide />);

    expect(screen.getByRole("heading", { name: "Connect. Start building." })).toBeVisible();

    state.authLoading = false;
    setServerRootPath("/team-x/");
    rerender(<DevelopersGuide />);

    const navigation = within(screen.getByRole("navigation", { name: "Main navigation" }));
    expect(navigation.getByRole("link", { name: "Models" })).toHaveAttribute("href", "/team-x/ui/models/");
    expect(navigation.getByRole("link", { name: "Developers" })).toHaveAttribute("href", "/team-x/ui/developers/");
    expect(navigation.getByRole("link", { name: "Pricing" })).toHaveAttribute("href", "/team-x/ui/pricing/");
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute("href", "/team-x/ui/signup/");
    expect(screen.getByRole("link", { name: "Manage API keys" })).toHaveAttribute("href", "/team-x/ui/api-keys/");
    expect(screen.getByRole("link", { name: "View request history" })).toHaveAttribute("href", "/team-x/ui/logs/");
    expect(screen.getByRole("link", { name: "Open billing" })).toHaveAttribute("href", "/team-x/ui/billing/");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("https://gateway.example/team-x/v1/chat/completions");
  });

  it("copies the selected model API example and resets feedback when the language changes", async () => {
    const user = userEvent.setup();
    render(<DevelopersGuide />);

    await user.click(screen.getByRole("tab", { name: "Anthropic Messages" }));
    await user.click(screen.getByRole("button", { name: "JavaScript" }));
    await user.click(screen.getByRole("button", { name: "Copy request example" }));

    expect(await screen.findByText("Request example copied.")).toBeVisible();
    expect(await navigator.clipboard.readText()).toContain("https://gateway.example/team-x/v1/messages");
    expect(await navigator.clipboard.readText()).toContain("YOUR_API_KEY");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("max_tokens");

    await user.click(screen.getByRole("button", { name: "Python" }));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("urllib.request");
    screen.getByRole("tab", { name: "Anthropic Messages" }).focus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "OpenAI-compatible" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("/v1/chat/completions");
  });
});
