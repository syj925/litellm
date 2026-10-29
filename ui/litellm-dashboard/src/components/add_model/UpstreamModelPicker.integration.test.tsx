import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FormProvider, useForm, useWatch } from "react-hook-form";
import type { MountedFormValues } from "@/components/common_components/MountedFormField";
import UpstreamModelPicker from "./UpstreamModelPicker";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

function Harness({
  provider = "OpenAI",
  credentialName = "",
  apiBase = "https://upstream.example/custom/v1",
}: {
  provider?: string;
  credentialName?: string;
  apiBase?: string;
}) {
  const form = useForm<MountedFormValues>({
    defaultValues: {
      api_base: apiBase,
      api_key: "test-credential",
      litellm_credential_name: credentialName,
      model: ["openai/existing-model"],
      model_mappings: [{ public_name: "edited-public-name", litellm_model: "openai/existing-model" }],
      model_name: "old-single-model",
    },
  });
  const model = useWatch({ control: form.control, name: "model" });
  const mappings = useWatch({ control: form.control, name: "model_mappings" });
  const modelName = useWatch({ control: form.control, name: "model_name" });
  return (
    <FormProvider {...form}>
      <UpstreamModelPicker selectedProvider={provider} accessToken="test-admin-session" canDiscover />
      <button type="button" onClick={() => form.setValue("api_base", "https://other-upstream.example/v1")}>
        Change connection
      </button>
      <output aria-label="Current model selection">{JSON.stringify({ model, mappings, modelName })}</output>
    </FormProvider>
  );
}

afterEach(() => vi.unstubAllGlobals());

it("fetches models, selects all search results and imports without replacing edited public mappings", async () => {
  const fetchModels = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        models: ["alpha", "team/alpha", "beta"],
        count: 3,
        protocol: "openai",
        source: "https://upstream.example/custom/v1/models",
        truncated: false,
      }),
      { status: 200 },
    ),
  );
  vi.stubGlobal("fetch", fetchModels);
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Fetch upstream models" }));
  const search = await screen.findByRole("textbox", { name: "Search upstream models" });
  fireEvent.change(search, { target: { value: "alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "Select all results" }));
  expect(screen.getByText("2 selected · 2 of 3 models shown")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Import selected" }));
  expect(screen.getByRole("status", { name: "Current model selection" })).toHaveTextContent(
    JSON.stringify({
      model: ["openai/existing-model", "openai/alpha", "openai/team/alpha"],
      mappings: [
        { public_name: "edited-public-name", litellm_model: "openai/existing-model" },
        { public_name: "alpha", litellm_model: "openai/alpha" },
        { public_name: "team/alpha", litellm_model: "openai/team/alpha" },
      ],
    }),
  );
  const [url, options] = fetchModels.mock.calls[0];
  expect(url).toBe("/retail/admin/model-discovery");
  expect(JSON.parse(options.body)).toEqual({
    provider: "openai",
    upstream_api_base: "https://upstream.example/custom/v1",
    upstream_api_key: "test-credential",
  });
  expect(options.headers.Authorization).toBe("Bearer test-admin-session");
});

it("aborts the previous discovery when the dialog closes", async () => {
  const fetchModels = vi.fn().mockImplementation(() => new Promise(() => {}));
  vi.stubGlobal("fetch", fetchModels);
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Fetch upstream models" }));
  expect(screen.getByText("Fetching upstream models")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Change connection" }));
  expect(fetchModels.mock.calls[0][1].signal.aborted).toBe(true);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("ignores an old response after switching providers and fetching a new list", async () => {
  let resolveOld: (value: Response) => void = () => {
    throw new Error("The first discovery did not start");
  };
  const fetchModels = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ models: ["new-provider-model"], count: 1, truncated: false }), { status: 200 }),
    );
  vi.stubGlobal("fetch", fetchModels);
  const { rerender } = render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Fetch upstream models" }));
  rerender(<Harness provider="Anthropic" />);
  expect(fetchModels.mock.calls[0][1].signal.aborted).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Fetch upstream models" }));
  expect(await screen.findByRole("checkbox", { name: "Select new-provider-model" })).toBeInTheDocument();
  await act(async () => {
    resolveOld(new Response(JSON.stringify({ models: ["old-provider-model"] }), { status: 200 }));
  });
  expect(screen.queryByRole("checkbox", { name: "Select old-provider-model" })).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Select new-provider-model" })).toBeInTheDocument();
});

it("uses saved credentials without forwarding stale hidden connection fields", async () => {
  const fetchModels = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify({ models: [], count: 0, truncated: false }), { status: 200 }));
  vi.stubGlobal("fetch", fetchModels);
  render(<Harness credentialName="saved-provider-connection" />);
  fireEvent.click(screen.getByRole("button", { name: "Fetch upstream models" }));
  expect(await screen.findByText("No upstream models found")).toBeInTheDocument();
  expect(JSON.parse(fetchModels.mock.calls[0][1].body)).toEqual({
    provider: "openai",
    credential_name: "saved-provider-connection",
  });
});

it("requires an explicit base URL for an OpenAI-compatible third-party connection", () => {
  render(<Harness provider="OpenAI_Compatible" apiBase="" />);
  expect(screen.getByRole("button", { name: "Fetch upstream models" })).toBeDisabled();
});
