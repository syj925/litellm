import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleAddModelSubmit, prepareModelAddRequest } from "./handle_add_model_submit";
import { modelCreateCall } from "../networking";
import { retailRequest } from "../retail/api";

vi.mock("../networking", () => ({
  modelCreateCall: vi.fn(),
}));

vi.mock("../retail/api", () => ({ retailRequest: vi.fn() }));

describe("prepareModelAddRequest", () => {
  it("converts all token prices once for every model without mutating the form", async () => {
    const values = {
      model_mappings: [
        { public_name: "first", litellm_model: "openai/first" },
        { public_name: "second", litellm_model: "openai/second" },
      ],
      input_cost_per_token: "2",
      output_cost_per_token: "10",
      cache_read_input_token_cost: "0.2",
      cache_creation_input_token_cost: "2.5",
      model_name: "stale-single-model",
    };
    const original = structuredClone(values);
    const deployments = await prepareModelAddRequest(values, "token", null);
    expect(deployments).toHaveLength(2);
    expect(deployments!.map((deployment) => deployment.litellmParamsObj.model)).toEqual([
      "openai/first",
      "openai/second",
    ]);
    for (const deployment of deployments!) {
      expect(deployment.litellmParamsObj).toMatchObject({
        input_cost_per_token: 0.000002,
        output_cost_per_token: 0.00001,
        cache_creation_input_token_cost: 0.0000025,
      });
      expect(deployment.litellmParamsObj.cache_read_input_token_cost).toBeCloseTo(0.0000002, 15);
    }
    expect(values).toEqual(original);
    expect(await prepareModelAddRequest(values, "token", null)).toEqual(deployments);
  });

  it("uses the same default cache read price for each model and preserves explicit free pricing", async () => {
    const deployments = await prepareModelAddRequest(
      {
        model_mappings: [
          { public_name: "first", litellm_model: "first" },
          { public_name: "second", litellm_model: "second" },
        ],
        input_cost_per_token: "2",
        output_cost_per_token: "0",
        cache_creation_input_token_cost: "0",
      },
      "token",
      null,
    );
    expect(deployments).toHaveLength(2);
    for (const deployment of deployments!) {
      expect(deployment.litellmParamsObj).toMatchObject({
        input_cost_per_token: 0.000002,
        output_cost_per_token: 0,
        cache_read_input_token_cost: 0.000002,
        cache_creation_input_token_cost: 0,
      });
    }
  });

  it("keeps the selected routing model when the old single-name field was cleared", async () => {
    const deployments = await prepareModelAddRequest(
      {
        model_name: undefined,
        model_mappings: [{ public_name: "friendly", litellm_model: "openai/upstream-model" }],
      },
      "token",
      null,
    );
    expect(deployments?.[0].litellmParamsObj.model).toBe("openai/upstream-model");
    expect(deployments?.[0].modelName).toBe("friendly");
  });

  it("returns deployment data for the most basic form", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      base_model: "gpt-4",
      team_id: "team-123",
      model_access_group: ["group-1"],
      input_cost_per_token: "2000000",
      output_cost_per_token: "1000000",
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelName).toBe("Public Model");
    expect(deployment.litellmParamsObj.model).toBe("custom-model-name");
    expect(deployment.litellmParamsObj.input_cost_per_token).toBe(2);
    expect(deployment.litellmParamsObj.output_cost_per_token).toBe(1);
    expect(deployment.modelInfoObj.base_model).toBe("gpt-4");
    expect(deployment.modelInfoObj.access_groups).toEqual(["group-1"]);
    expect(deployment.modelInfoObj.team_id).toBe("team-123");
  });

  it("uses a lowercase fallback for unrecognized custom providers", async () => {
    const fallbackValues = {
      model_mappings: [
        {
          public_name: "Petals Model",
          litellm_model: "petals/model",
        },
      ],
      model_name: "petals/model",
      custom_llm_provider: "Petals",
    };

    const deployments = await prepareModelAddRequest({ ...fallbackValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.custom_llm_provider).toBe("petals");
  });

  it("ignores litellm_credential_name inside LiteLLM Params JSON", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      litellm_credential_name: "selected-credential",
      litellm_extra_params: JSON.stringify({
        litellm_credential_name: "from-json",
        timeout: 5,
      }),
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.litellm_credential_name).toBe("selected-credential");
    expect(deployment.litellmParamsObj.timeout).toBe(5);
  });

  it("keeps litellm_credential_name from LiteLLM Params JSON when no credential is selected", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      litellm_extra_params: JSON.stringify({
        litellm_credential_name: "from-json",
        timeout: 5,
      }),
      litellm_credential_name: null,
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.litellm_credential_name).toBe("from-json");
    expect(deployment.litellmParamsObj.timeout).toBe(5);
  });

  it("saves the selected mode under model_info", async () => {
    const formValues = {
      model_mappings: [{ public_name: "Jev", litellm_model: "typesafe/jev-latest" }],
      mode: "evaluation",
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelInfoObj.mode).toBe("evaluation");
    expect(deployment.litellmParamsObj).not.toHaveProperty("mode");
  });

  it.each([
    ["OpenAI", "openai/*"],
    ["Azure_AI_Studio", "azure_ai/*"],
    ["Petals", "petals/*"],
  ])("composes wildcard names for the all-model selection", async (custom_llm_provider, wildcardModel) => {
    const formValues = {
      model_mappings: [],
      model: "all-wildcard",
      custom_llm_provider,
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelName).toBe(wildcardModel);
    expect(deployment.litellmParamsObj.model).toBe(wildcardModel);
  });
});

describe("named channel submission", () => {
  const values = {
    retail_channel: { mode: "new", name: "  Supplier A  ", pool_id: "pool-a" },
    api_base: "https://supplier.example.test",
    model_mappings: [
      { public_name: "shared", litellm_model: "anthropic/shared" },
      { public_name: "other", litellm_model: "anthropic/other" },
    ],
  };
  beforeEach(() => vi.clearAllMocks());

  it("saves every selected model in one channel request without forwarding channel fields upstream", async () => {
    vi.mocked(retailRequest).mockResolvedValue({ id: "channel-a", name: "Supplier A", status: "active" });
    const form = { resetFields: vi.fn() };
    const refresh = vi.fn();
    expect(await handleAddModelSubmit(values, "token", form, refresh)).toBe(true);
    expect(retailRequest).toHaveBeenCalledExactlyOnceWith("/admin/channels", "token", {
      method: "POST",
      body: JSON.stringify({
        name: "Supplier A",
        pool_id: "pool-a",
        deployments: [
          {
            model_name: "shared",
            litellm_params: { model: "anthropic/shared", api_base: values.api_base },
            model_info: {},
          },
          {
            model_name: "other",
            litellm_params: { model: "anthropic/other", api_base: values.api_base },
            model_info: {},
          },
        ],
      }),
    });
    expect(modelCreateCall).not.toHaveBeenCalled();
    expect(form.resetFields).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("appends a batch to the chosen channel without sending a replacement pool or name", async () => {
    vi.mocked(retailRequest).mockResolvedValue({ id: "channel/a", name: "Supplier A", status: "active" });
    expect(
      await handleAddModelSubmit(
        { ...values, retail_channel: { mode: "existing", channel_id: "channel/a" } },
        "token",
        { resetFields: vi.fn() },
      ),
    ).toBe(true);
    const [path, , options] = vi.mocked(retailRequest).mock.calls[0];
    expect(path).toBe("/admin/channels/channel%2Fa/deployments");
    expect(JSON.parse(String(options?.body))).toEqual({
      deployments: [
        {
          model_name: "shared",
          litellm_params: { model: "anthropic/shared", api_base: values.api_base },
          model_info: {},
        },
        {
          model_name: "other",
          litellm_params: { model: "anthropic/other", api_base: values.api_base },
          model_info: {},
        },
      ],
    });
  });

  it("keeps the completed form and avoids native fallback when channel provisioning fails", async () => {
    vi.mocked(retailRequest).mockRejectedValue(new Error("Channel setup could not be completed"));
    const form = { resetFields: vi.fn() };
    const refresh = vi.fn();
    expect(await handleAddModelSubmit(values, "token", form, refresh)).toBe(false);
    expect(form.resetFields).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
    expect(modelCreateCall).not.toHaveBeenCalled();
  });

  it("does not report a channel error state as a successful model creation", async () => {
    vi.mocked(retailRequest).mockResolvedValue({ id: "channel-a", name: "Supplier A", status: "error" });
    const form = { resetFields: vi.fn() };
    expect(await handleAddModelSubmit(values, "token", form)).toBe(false);
    expect(form.resetFields).not.toHaveBeenCalled();
    expect(modelCreateCall).not.toHaveBeenCalled();
  });

  it("rejects a blank channel identity before any create call", async () => {
    expect(
      await handleAddModelSubmit(
        { ...values, retail_channel: { mode: "new", name: " ", pool_id: "pool-a" } },
        "token",
        { resetFields: vi.fn() },
      ),
    ).toBe(false);
    expect(retailRequest).not.toHaveBeenCalled();
    expect(modelCreateCall).not.toHaveBeenCalled();
  });

  it("retains native model creation when the channel feature is absent", async () => {
    vi.mocked(modelCreateCall).mockResolvedValue({});
    const { retail_channel, ...native } = values;
    expect(await handleAddModelSubmit(native, "token", { resetFields: vi.fn() })).toBe(true);
    expect(modelCreateCall).toHaveBeenCalledTimes(2);
    expect(retailRequest).not.toHaveBeenCalled();
  });
});
