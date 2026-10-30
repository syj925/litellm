import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React, { useEffect, useRef } from "react";
import { useFormContext, useWatch } from "react-hook-form";
import { describe, expect, it } from "vitest";
import { MountedFormHost } from "../../../tests/mounted-form-host";
import type { MountedFormValues } from "../common_components/MountedFormField";
import ConditionalPublicModelName from "./conditional_public_model_name";
import { Providers } from "../provider_info_helpers";

const WRITE_BUDGET = 20;

const LoopGuard: React.FC = () => {
  const form = useFormContext<MountedFormValues>();
  const mappings = useWatch({ control: form.control, name: "model_mappings" });
  const writes = useRef(0);

  useEffect(() => {
    writes.current += 1;
    if (writes.current > WRITE_BUDGET) {
      throw new Error(`model_mappings changed ${WRITE_BUDGET}+ times: the mapping effects are looping`);
    }
  }, [mappings]);

  return null;
};

const MappingState: React.FC = () => {
  const form = useFormContext<MountedFormValues>();
  const mappings = useWatch({ control: form.control, name: "model_mappings" });
  return <output data-testid="mapping-state">{JSON.stringify(mappings)}</output>;
};

describe("ConditionalPublicModelName", () => {
  it("fills new mapping rows with plain public defaults and keeps an existing alias and target exact", () => {
    render(
      <MountedFormHost
        defaultValues={{
          custom_llm_provider: Providers.OpenAI,
          model: ["openai/existing", "openai/vendor/model-name", "custom/model-name"],
          model_mappings: [{ public_name: "customer-alias", litellm_model: "openai/existing" }],
        }}
      >
        <ConditionalPublicModelName />
        <MappingState />
        <LoopGuard />
      </MountedFormHost>,
    );
    expect(screen.getByDisplayValue("customer-alias")).toBeInTheDocument();
    expect(screen.getByDisplayValue("vendor/model-name")).toBeInTheDocument();
    expect(screen.getByDisplayValue("custom/model-name")).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId("mapping-state").textContent!)).toEqual([
      { public_name: "customer-alias", litellm_model: "openai/existing" },
      { public_name: "vendor/model-name", litellm_model: "openai/vendor/model-name" },
      { public_name: "custom/model-name", litellm_model: "custom/model-name" },
    ]);
  });

  it("shows the unprefixed upstream name while preserving the mapping value and editable public alias", async () => {
    const user = userEvent.setup();
    render(
      <MountedFormHost
        defaultValues={{
          custom_llm_provider: Providers.Anthropic,
          model: ["anthropic/claude-test"],
          model_mappings: [{ public_name: "customer-alias", litellm_model: "anthropic/claude-test" }],
        }}
      >
        <ConditionalPublicModelName />
        <MappingState />
        <LoopGuard />
      </MountedFormHost>,
    );
    expect(screen.getByText("claude-test")).toBeInTheDocument();
    expect(screen.queryByText("anthropic/claude-test")).not.toBeInTheDocument();
    const input = screen.getByDisplayValue("customer-alias");
    await user.type(input, "-prod");
    expect(input).toHaveFocus();
    expect(input).toHaveValue("customer-alias-prod");
    expect(JSON.parse(screen.getByTestId("mapping-state").textContent!)).toEqual([
      { public_name: "customer-alias-prod", litellm_model: "anthropic/claude-test" },
    ]);
  });

  it("removes only the selected adapter from the mapping display", () => {
    render(
      <MountedFormHost
        defaultValues={{
          custom_llm_provider: Providers.Openrouter,
          model: ["openrouter/anthropic/claude-test", "custom/vendor-model"],
          model_mappings: [
            { public_name: "claude-alias", litellm_model: "openrouter/anthropic/claude-test" },
            { public_name: "vendor-alias", litellm_model: "custom/vendor-model" },
          ],
        }}
      >
        <ConditionalPublicModelName />
      </MountedFormHost>,
    );
    expect(screen.getByText("anthropic/claude-test")).toBeInTheDocument();
    expect(screen.getByText("custom/vendor-model")).toBeInTheDocument();
  });

  it("should render", () => {
    render(
      <MountedFormHost
        defaultValues={{
          model: ["gpt-4"],
          model_mappings: [
            {
              public_name: "gpt-4",
              litellm_model: "gpt-4",
            },
          ],
        }}
      >
        <ConditionalPublicModelName />
      </MountedFormHost>,
    );

    expect(screen.getByText("Model Mappings")).toBeInTheDocument();
    expect(screen.getByText("Public Model Name")).toBeInTheDocument();
    expect(screen.getByText("LiteLLM Model Name")).toBeInTheDocument();
  });

  it("settles after rewriting the custom placeholder mapping to the entered model name", () => {
    render(
      <MountedFormHost
        defaultValues={{
          model: ["custom"],
          custom_model_name: "my-custom-model",
          model_mappings: [
            {
              public_name: "custom",
              litellm_model: "custom",
            },
          ],
        }}
      >
        <ConditionalPublicModelName />
        <LoopGuard />
      </MountedFormHost>,
    );

    expect(screen.getByDisplayValue("my-custom-model")).toBeInTheDocument();
    expect(screen.getByText("my-custom-model")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("custom")).not.toBeInTheDocument();
  });

  it("keeps the public name input focused across keystrokes", async () => {
    const user = userEvent.setup();
    render(
      <MountedFormHost
        defaultValues={{
          model: ["gpt-4"],
          model_mappings: [{ public_name: "gpt-4", litellm_model: "gpt-4" }],
        }}
      >
        <ConditionalPublicModelName />
      </MountedFormHost>,
    );

    const input = screen.getByDisplayValue("gpt-4");
    await user.type(input, "-prod");

    expect(input).toHaveValue("gpt-4-prod");
    expect(input).toHaveFocus();
  });
});
