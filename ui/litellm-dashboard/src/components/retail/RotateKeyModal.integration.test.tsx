import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import RotateKeyModal from "./RotateKeyModal";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

afterEach(() => vi.unstubAllGlobals());

it("requires confirmation, sends only the target key, and keeps the new secret out of parent key data", async () => {
  const request = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        key: "sk-local-ui-test-secret",
        token_id: "new-key-id",
        key_name: "sk-...test",
        rotated_at: "2026-01-01T00:00:00Z",
      }),
      { status: 200 },
    ),
  );
  vi.stubGlobal("fetch", request);
  const onRotated = vi.fn();
  const onClose = vi.fn();
  const view = render(
    <RotateKeyModal
      accessToken="test-session"
      tokenId="old-key-id"
      keyAlias="Production API"
      onClose={onClose}
      onRotated={onRotated}
    />,
  );
  expect(screen.getByRole("button", { name: "Rotate key" })).toBeDisabled();
  expect(screen.getByText(/Rotation does not renew an expired key/)).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Rotate key" }));
  expect(await screen.findByRole("textbox", { name: "New API key" })).toHaveValue("sk-local-ui-test-secret");
  expect(request).toHaveBeenCalledOnce();
  expect(request.mock.calls[0][0]).toBe("/retail/keys/rotate");
  expect(JSON.parse(String(request.mock.calls[0][1].body))).toEqual({ key: "old-key-id" });
  expect(onRotated).toHaveBeenCalledWith({
    token_id: "new-key-id",
    key_name: "sk-...test",
    rotated_at: "2026-01-01T00:00:00Z",
  });
  expect(screen.getByRole("button", { name: "Copy new API key" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(onClose).toHaveBeenCalledOnce();
  view.unmount();
  render(
    <RotateKeyModal
      accessToken="test-session"
      tokenId="new-key-id"
      keyAlias="Production API"
      onClose={vi.fn()}
      onRotated={vi.fn()}
    />,
  );
  expect(screen.queryByDisplayValue("sk-local-ui-test-secret")).not.toBeInTheDocument();
});

it("shows a server permission denial without replacing the current key", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: "You are not authorized to rotate this API key" }), { status: 403 }),
      ),
  );
  const onRotated = vi.fn();
  render(
    <RotateKeyModal
      accessToken="test-session"
      tokenId="another-key"
      keyAlias="Other API"
      onClose={vi.fn()}
      onRotated={onRotated}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Rotate key" }));
  expect(await screen.findByText("You are not authorized to rotate this API key")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "New API key" })).not.toBeInTheDocument();
  expect(onRotated).not.toHaveBeenCalled();
});
