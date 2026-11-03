import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setServerRootPath } from "@/lib/serverRootPath";
import GatewayLanding from "./GatewayLanding";

const state = vi.hoisted(() => ({ authLoading: false, proxyBaseUrl: "https://gateway.example/team-x" }));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ authLoading: state.authLoading, token: null, accessToken: null, userRole: "" }),
}));

vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => state.proxyBaseUrl }));

vi.mock("next/font/local", () => ({ default: () => ({ variable: "landing-font", className: "landing-font" }) }));

type ObservedEntry = Pick<IntersectionObserverEntry, "target" | "isIntersecting" | "boundingClientRect">;
type ObserverCallback = (entries: ObservedEntry[]) => void;
const observedTargets = new Map<Element, ObserverCallback>();

class ObserverStub {
  constructor(private readonly callback: ObserverCallback) {}
  observe = vi.fn((target: Element) => observedTargets.set(target, this.callback));
  unobserve = vi.fn((target: Element) => observedTargets.delete(target));
  disconnect = vi.fn();
}

function setArtVisible(visible: boolean) {
  const observation = [...observedTargets].find(([target]) => target.id === "one-brand-motion");
  expect(observation).toBeDefined();
  if (!observation) return;
  const [target, notify] = observation;
  act(() => notify([{ target, isIntersecting: visible, boundingClientRect: target.getBoundingClientRect() }]));
}

beforeEach(() => {
  observedTargets.clear();
  vi.stubEnv("NODE_ENV", "production");
  vi.stubGlobal("IntersectionObserver", ObserverStub);
  setServerRootPath("/team-x");
  state.authLoading = false;
  state.proxyBaseUrl = "https://gateway.example/team-x";
});

afterEach(() => {
  setServerRootPath("/");
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("public gateway homepage", () => {
  it("renders for an anonymous visitor and updates deployment-prefixed destinations after bootstrap", () => {
    state.authLoading = true;
    setServerRootPath("/");
    const { rerender } = render(<GatewayLanding />);

    expect(screen.getByRole("heading", { name: "One connection. More possibilities." })).toBeVisible();

    setServerRootPath("/team-x/");
    state.authLoading = false;
    rerender(<GatewayLanding />);

    for (const link of screen.getAllByRole("link", { name: "Explore models" })) {
      expect(link).toHaveAttribute("href", "/team-x/ui/models/");
    }
    for (const link of screen.getAllByRole("link", { name: "Sign in" })) {
      expect(link).toHaveAttribute("href", "/team-x/ui/login/");
    }
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute("href", "/team-x/ui/signup/");
    expect(screen.getByRole("link", { name: "AI Gateway home" })).toHaveAttribute("href", "/team-x/ui/welcome/");
    expect(screen.getByRole("link", { name: "Models", exact: true })).toHaveAttribute("href", "/team-x/ui/models/");
    for (const link of screen.getAllByRole("link", { name: "Developers", exact: true })) {
      expect(link).toHaveAttribute("href", "/team-x/ui/developers/");
    }
    for (const link of screen.getAllByRole("link", { name: "Pricing", exact: true })) {
      expect(link).toHaveAttribute("href", "/team-x/ui/pricing/");
    }
    expect(screen.getByRole("tabpanel")).toHaveTextContent("https://gateway.example/team-x/v1/chat/completions");
  });

  it("copies the selected API format and language using the actual gateway endpoint", async () => {
    const user = userEvent.setup();
    render(<GatewayLanding />);

    await user.click(screen.getByRole("tab", { name: "Anthropic Messages" }));
    await user.click(screen.getByRole("button", { name: "JavaScript" }));

    expect(screen.getByRole("tab", { name: "Anthropic Messages" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("https://gateway.example/team-x/v1/messages");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("x-api-key");
    await user.click(screen.getByRole("button", { name: "Copy request example" }));
    expect(await screen.findByText("Request example copied.")).toBeVisible();
    expect(await navigator.clipboard.readText()).toContain("https://gateway.example/team-x/v1/messages");

    screen.getByRole("tab", { name: "Anthropic Messages" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "OpenAI-compatible" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("https://gateway.example/team-x/v1/chat/completions");
  });

  it("filters capabilities and keeps motion and mobile navigation keyboard-operable", async () => {
    const user = userEvent.setup();
    render(<GatewayLanding />);

    await user.click(screen.getByRole("button", { name: "Language", exact: true }));
    expect(screen.getByRole("heading", { name: "Language", exact: true })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Vision", exact: true })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Multimodal", exact: true }));
    expect(screen.getByRole("heading", { name: "Vision", exact: true })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Language", exact: true })).not.toBeInTheDocument();

    screen.getByRole("button", { name: "Pause ONE animation" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Resume ONE animation" })).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Pause ONE animation" })).toHaveAttribute("aria-pressed", "false");

    const menu = screen.getByText("Menu", { exact: true });
    menu.focus();
    await user.keyboard("{Enter}");
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Mobile navigation")).not.toHaveAttribute("hidden");
    const mobileNavigation = screen.getByLabelText("Mobile navigation");
    expect(within(mobileNavigation).getByText("Models", { exact: true })).toHaveAttribute("href", "/team-x/ui/models/");
    expect(within(mobileNavigation).getByText("Developers", { exact: true })).toHaveAttribute(
      "href",
      "/team-x/ui/developers/",
    );
    expect(within(mobileNavigation).getByText("Pricing", { exact: true })).toHaveAttribute(
      "href",
      "/team-x/ui/pricing/",
    );
    await user.keyboard("{Escape}");
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveFocus();
  });

  it("keeps a manual pause when the artwork leaves and returns to the viewport", async () => {
    const user = userEvent.setup();
    render(<GatewayLanding />);
    const hero = screen.getByRole("region", { name: "One connection. More possibilities." });

    setArtVisible(true);
    expect(hero).toHaveAttribute("data-motion", "running");
    await user.click(screen.getByRole("button", { name: "Pause ONE animation" }));
    expect(hero).toHaveAttribute("data-motion", "paused");

    setArtVisible(false);
    expect(hero).toHaveAttribute("data-motion", "paused");
    setArtVisible(true);
    expect(hero).toHaveAttribute("data-motion", "paused");
    await user.click(screen.getByRole("button", { name: "Resume ONE animation" }));
    expect(hero).toHaveAttribute("data-motion", "running");
  });

  it("restores automatic playback after page restoration while preserving a manual pause", async () => {
    const user = userEvent.setup();
    render(<GatewayLanding />);
    const hero = screen.getByRole("region", { name: "One connection. More possibilities." });
    setArtVisible(true);

    fireEvent(window, new PageTransitionEvent("pagehide", { persisted: true }));
    expect(hero).toHaveAttribute("data-motion", "hidden");
    fireEvent(window, new PageTransitionEvent("pageshow", { persisted: true }));
    expect(hero).toHaveAttribute("data-motion", "running");

    await user.click(screen.getByRole("button", { name: "Pause ONE animation" }));
    fireEvent(window, new PageTransitionEvent("pagehide", { persisted: true }));
    fireEvent(window, new PageTransitionEvent("pageshow", { persisted: true }));
    expect(hero).toHaveAttribute("data-motion", "paused");
    expect(screen.getByRole("button", { name: "Resume ONE animation" })).toHaveAttribute("aria-pressed", "true");
  });
});
