import { fireEvent, render, screen } from "@testing-library/react";
import { useFormContext, useWatch } from "react-hook-form";
import { describe, expect, it } from "vitest";
import { getPlaceholder, Providers } from "../provider_info_helpers";
import { MountedFormHost } from "../../../tests/mounted-form-host";
import LiteLLMModelNameField from "./litellm_model_name";

function SelectionState() {
  const { control } = useFormContext();
  const [model, model_mappings, model_name] = useWatch({ control, name: ["model", "model_mappings", "model_name"] });
  return <output data-testid="selection-state">{JSON.stringify({ model, model_mappings, model_name })}</output>;
}

describe("LitellmModelNameField", () => {
  it("selects the catalog at once, preserves edited public names and clears the selection", () => {
    render(
      <MountedFormHost
        defaultValues={{
          model: ["first"],
          model_name: "stale-single-model",
          model_mappings: [{ public_name: "my-alias", litellm_model: "first" }],
        }}
      >
        <LiteLLMModelNameField
          selectedProvider={Providers.OpenAI}
          providerModels={["first", "second"]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Select all 2 catalog models" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["first", "second"],
      model_mappings: [
        { public_name: "my-alias", litellm_model: "first" },
        { public_name: "second", litellm_model: "second" },
      ],
    });
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({ model: [], model_mappings: [] });
  });

  it.each(["OpenAI_Compatible", Providers.OpenAI_Compatible, "Ollama"])(
    "shows imported model arrays as a multi-select for %s",
    (provider) => {
      render(
        <MountedFormHost defaultValues={{ model: ["first", "second"] }}>
          <LiteLLMModelNameField
            selectedProvider={provider}
            providerModels={["unavailable-catalog-model"]}
            getPlaceholder={getPlaceholder}
          />
        </MountedFormHost>,
      );
      expect(screen.getByRole("combobox")).toBeInTheDocument();
      expect(screen.getByText("first")).toBeInTheDocument();
      expect(screen.getByText("second")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Select all/ })).not.toBeInTheDocument();
    },
  );

  it("should render", () => {
    render(
      <MountedFormHost>
        <LiteLLMModelNameField
          selectedProvider={Providers.OpenAI}
          providerModels={[]}
          getPlaceholder={getPlaceholder}
        />
      </MountedFormHost>,
    );
    expect(screen.getByText("LiteLLM Model Name(s)")).toBeInTheDocument();
  });

  it("should show Azure placeholder as 'my-deployment'", () => {
    render(
      <MountedFormHost>
        <LiteLLMModelNameField selectedProvider={Providers.Azure} providerModels={[]} getPlaceholder={getPlaceholder} />
      </MountedFormHost>,
    );
    expect(screen.getByPlaceholderText("my-deployment")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("gpt-3.5-turbo")).not.toBeInTheDocument();
  });
});
