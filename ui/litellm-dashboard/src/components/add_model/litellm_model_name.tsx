import React from "react";
import { MultiSelect } from "@/components/shared/MultiSelect";
import { useFormContext, useWatch } from "react-hook-form";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { requiredRule } from "../common_components/formRules";
import { labelWithHint } from "@/components/shared/form/LabelWithHint";
import { MountedFormField, type MountedFormValues } from "../common_components/MountedFormField";
import { Providers } from "../provider_info_helpers";
import { buildModelChoices, modelDisplayName } from "./modelDisplayName";

interface LiteLLMModelNameFieldProps {
  selectedProvider: string | null;
  providerModels: string[];
  discoveredModels?: readonly string[];
  getPlaceholder: (provider: string) => string;
}

const LiteLLMModelNameField: React.FC<LiteLLMModelNameFieldProps> = ({
  selectedProvider,
  providerModels,
  discoveredModels = [],
  getPlaceholder,
}) => {
  const form = useFormContext<MountedFormValues>();
  const modelValue = useWatch({ control: form.control, name: "model" });
  const selectedModels = (Array.isArray(modelValue) ? modelValue : [modelValue]).filter(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
  const isAzure = selectedProvider === Providers.Azure || selectedProvider === "Azure";
  const usesCustomCatalog = ["OpenAI_Compatible", Providers.OpenAI_Compatible, "Ollama", Providers.Ollama].includes(
    selectedProvider ?? "",
  );
  const availableModels = Array.from(
    new Set(
      [...(usesCustomCatalog ? [] : providerModels), ...discoveredModels].filter(
        (model) => model && !["custom", "all-wildcard"].includes(model),
      ),
    ),
  );
  const choices = buildModelChoices(
    availableModels,
    selectedModels.filter((model) => !["custom", "all-wildcard"].includes(model)),
    selectedProvider,
  );

  const handleModelChange = (value: string | string[]) => {
    const values = Array.isArray(value) ? value : [value];
    form.setValue("model_name", undefined);
    if (values.includes("all-wildcard")) {
      form.setValue("model", ["all-wildcard"], { shouldDirty: true });
      form.setValue("model_mappings", []);
    } else {
      const previous = (form.getValues("model_mappings") ?? []) as { public_name: string; litellm_model: string }[];
      const mappings = values.map((model) => {
        const target = isAzure ? `azure/${model}` : model;
        return (
          previous.find((mapping) => mapping.litellm_model === target) ?? {
            public_name: modelDisplayName(model, selectedProvider),
            litellm_model: target,
          }
        );
      });
      form.setValue("model", values, { shouldDirty: true, shouldValidate: true });
      form.setValue("model_mappings", mappings, { shouldDirty: true });
    }
  };

  const handleAzureDeploymentNameChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const deploymentName = e.target.value;

    // Create mapping with Azure-specific format
    const mappings = deploymentName
      ? [
          {
            public_name: deploymentName,
            litellm_model: `azure/${deploymentName}`,
          },
        ]
      : [];

    // Update both fields
    form.setValue("model", deploymentName);
    form.setValue("model_mappings", mappings);
  };

  // Handle custom model name changes
  const handleCustomModelNameChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const customName = e.target.value;

    // Immediately update the model mappings
    const currentMappings = (form.getValues("model_mappings") as any[]) || [];
    const updatedMappings = currentMappings.map((mapping: any) => {
      if (mapping.public_name === "custom" || mapping.litellm_model === "custom") {
        if (selectedProvider === Providers.Azure) {
          return {
            public_name: customName,
            litellm_model: `azure/${customName}`,
          };
        }
        return {
          public_name: customName,
          litellm_model: customName,
        };
      }
      return mapping;
    });

    form.setValue("model_mappings", updatedMappings);
  };

  return (
    <>
      <MountedFormField
        name="model"
        label={labelWithHint("LiteLLM Model Name(s)", "The model name LiteLLM will send to the LLM API")}
        required
        rules={{
          validate: {
            required: requiredRule(
              `Please enter ${selectedProvider === Providers.Azure ? "a deployment name" : "at least one model"}.`,
            ),
          },
        }}
        className="mb-0"
      >
        {(control) =>
          isAzure ? (
            <Input
              id={control.id}
              value={(control.value as string | undefined) ?? ""}
              onBlur={control.onBlur}
              placeholder={selectedProvider === null ? "Select a provider first" : getPlaceholder(selectedProvider)}
              onChange={(event) => {
                control.onChange(event);
                if (selectedProvider === Providers.Azure) {
                  handleAzureDeploymentNameChange(event);
                }
              }}
            />
          ) : selectedProvider ? (
            <MultiSelect
              id={control.id}
              placeholder="Select models"
              emptyText="No models found"
              value={
                Array.isArray(control.value)
                  ? control.value
                  : typeof control.value === "string" && control.value
                    ? [control.value]
                    : []
              }
              allowCustomValues
              onValueChange={(value: string[]) => {
                handleModelChange(value);
              }}
              options={[
                {
                  label: "Custom Model Name (Enter below)",
                  value: "custom",
                },
                {
                  label: `All ${selectedProvider ?? "provider"} Models (Wildcard)`,
                  value: "all-wildcard",
                },
                ...choices.options,
              ]}
              className="max-h-40 w-full overflow-y-auto"
            />
          ) : (
            <Input
              id={control.id}
              value={(control.value as string | undefined) ?? ""}
              onChange={control.onChange}
              onBlur={control.onBlur}
              placeholder={selectedProvider === null ? "Select a provider first" : getPlaceholder(selectedProvider)}
            />
          )
        }
      </MountedFormField>

      {!isAzure && selectedProvider && availableModels.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="xs" onClick={() => handleModelChange(choices.selectAllValues)}>
            Select all {choices.catalogCount} catalog models
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            disabled={selectedModels.length === 0}
            onClick={() => handleModelChange([])}
          >
            Clear selection
          </Button>
          <span className="text-xs text-muted-foreground">{selectedModels.length} selected</span>
        </div>
      )}

      {selectedModels.includes("custom") && (
        <MountedFormField
          name="custom_model_name"
          required
          rules={{ validate: { required: requiredRule("Please enter a custom model name.") } }}
          className="mt-2"
        >
          {(control) => (
            <Input
              id={control.id}
              value={(control.value as string | undefined) ?? ""}
              onBlur={control.onBlur}
              placeholder={
                selectedProvider === Providers.Azure ? "Enter Azure deployment name" : "Enter custom model name"
              }
              onChange={(event) => {
                control.onChange(event);
                handleCustomModelNameChange(event);
              }}
            />
          )}
        </MountedFormField>
      )}
      <div className="grid grid-cols-24">
        <p className="col-start-11 col-span-14 text-sm mb-3 mt-1">
          {selectedProvider === Providers.Azure
            ? "Your deployment name will be saved as the public model name, and LiteLLM will use 'azure/deployment-name' internally"
            : "The model name LiteLLM will send to the LLM API"}
        </p>
      </div>
    </>
  );
};

export default LiteLLMModelNameField;
