import { describe, expect, it, vi } from "vitest";
import { prepareModelAddRequest } from "./handle_add_model_submit";

vi.mock("../networking", () => ({
  modelCreateCall: vi.fn(),
}));

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
