import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  it("keeps every raw catalog candidate selectable and preserves existing aliases when selecting all", async () => {
    const user = userEvent.setup();
    render(
      <MountedFormHost
        defaultValues={{
          model: ["anthropic/claude-haiku"],
          model_mappings: [{ public_name: "private-alias", litellm_model: "anthropic/claude-haiku" }],
        }}
      >
        <LiteLLMModelNameField
          selectedProvider={Providers.Anthropic}
          providerModels={["claude-haiku", "anthropic/claude-haiku", "claude-opus"]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    await user.click(screen.getByRole("combobox"));
    const options = screen.getAllByRole("option", { name: "claude-haiku", exact: true });
    expect(options).toHaveLength(2);
    const unselected = options.find((option) => option.getAttribute("aria-selected") === "false");
    expect(unselected).toBeDefined();
    await user.click(unselected!);
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["anthropic/claude-haiku", "claude-haiku"],
      model_mappings: [
        { public_name: "private-alias", litellm_model: "anthropic/claude-haiku" },
        { public_name: "claude-haiku", litellm_model: "claude-haiku" },
      ],
    });
    await user.click(unselected!);
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!).model).toEqual(["anthropic/claude-haiku"]);
    const availableAgain = screen
      .getAllByRole("option", { name: "claude-haiku", exact: true })
      .find((option) => option.getAttribute("aria-selected") === "false");
    expect(availableAgain).toBeDefined();
    await user.click(availableAgain!);
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!).model).toEqual([
      "anthropic/claude-haiku",
      "claude-haiku",
    ]);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Select all 3 catalog models" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["anthropic/claude-haiku", "claude-haiku", "claude-opus"],
      model_mappings: [
        { public_name: "private-alias", litellm_model: "anthropic/claude-haiku" },
        { public_name: "claude-haiku", litellm_model: "claude-haiku" },
        { public_name: "claude-opus", litellm_model: "claude-opus" },
      ],
    });
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({ model: [], model_mappings: [] });
  });

  it("does not merge saved mappings with different public aliases but equivalent adapter names", () => {
    render(
      <MountedFormHost
        defaultValues={{
          model: ["anthropic/claude-haiku", "claude-haiku"],
          model_mappings: [
            { public_name: "first-alias", litellm_model: "anthropic/claude-haiku" },
            { public_name: "second-alias", litellm_model: "claude-haiku" },
          ],
        }}
      >
        <LiteLLMModelNameField
          selectedProvider={Providers.Anthropic}
          providerModels={["claude-haiku", "claude-opus"]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Select all 2 catalog models" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["anthropic/claude-haiku", "claude-haiku", "claude-opus"],
      model_mappings: [
        { public_name: "first-alias", litellm_model: "anthropic/claude-haiku" },
        { public_name: "second-alias", litellm_model: "claude-haiku" },
        { public_name: "claude-opus", litellm_model: "claude-opus" },
      ],
    });
  });

  it("shows plain catalog and imported chip labels while keeping adapter-prefixed values and aliases", async () => {
    const user = userEvent.setup();
    render(
      <MountedFormHost
        defaultValues={{
          model: ["anthropic/imported-model"],
          model_mappings: [{ public_name: "customer-alias", litellm_model: "anthropic/imported-model" }],
        }}
      >
        <LiteLLMModelNameField
          selectedProvider={Providers.Anthropic}
          providerModels={["anthropic/catalog-model"]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    expect(screen.getByText("imported-model")).toBeInTheDocument();
    expect(screen.queryByText("anthropic/imported-model")).not.toBeInTheDocument();
    await user.type(screen.getByRole("combobox"), "catalog-model");
    await user.click(await screen.findByRole("option", { name: "catalog-model", exact: true }));
    expect(screen.getByText("catalog-model")).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["anthropic/imported-model", "anthropic/catalog-model"],
      model_mappings: [
        { public_name: "customer-alias", litellm_model: "anthropic/imported-model" },
        { public_name: "catalog-model", litellm_model: "anthropic/catalog-model" },
      ],
    });
  });

  it("shows custom selected values without hiding the supplier's nested namespace", () => {
    render(
      <MountedFormHost defaultValues={{ model: ["openai/vendor/model-name", "custom/model-name"] }}>
        <LiteLLMModelNameField
          selectedProvider={Providers.OpenAI_Compatible}
          providerModels={[]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    expect(screen.getByText("vendor/model-name")).toBeInTheDocument();
    expect(screen.getByText("custom/model-name")).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["openai/vendor/model-name", "custom/model-name"],
    });
  });

  it("uses plain public defaults for bulk selection without changing supplier namespaces or existing aliases", () => {
    render(
      <MountedFormHost
        defaultValues={{
          model: ["openai/existing"],
          model_mappings: [{ public_name: "custom-public-alias", litellm_model: "openai/existing" }],
        }}
      >
        <LiteLLMModelNameField
          selectedProvider={Providers.OpenAI}
          providerModels={["openai/existing", "openai/vendor/model-name", "custom/model-name"]}
          getPlaceholder={getPlaceholder}
        />
        <SelectionState />
      </MountedFormHost>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Select all 3 catalog models" }));
    expect(JSON.parse(screen.getByTestId("selection-state").textContent!)).toEqual({
      model: ["openai/existing", "openai/vendor/model-name", "custom/model-name"],
      model_mappings: [
        { public_name: "custom-public-alias", litellm_model: "openai/existing" },
        { public_name: "vendor/model-name", litellm_model: "openai/vendor/model-name" },
        { public_name: "custom/model-name", litellm_model: "custom/model-name" },
      ],
    });
  });

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
