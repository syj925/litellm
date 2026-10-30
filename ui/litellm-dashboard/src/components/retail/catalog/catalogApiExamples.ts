import type { CatalogModel } from "./types";
import { getModelIdentity } from "./modelIdentity";

export type CatalogApiProtocol = "openai" | "anthropic";
export type CatalogApiLanguage = "curl" | "python" | "javascript";

export function catalogApiProtocols(model: CatalogModel): CatalogApiProtocol[] {
  if (model.mode === "embedding") return ["openai"];
  if (model.mode !== "chat") return [];
  return getModelIdentity(model.id, model.provider).providerSlug === "anthropic" ? ["openai", "anthropic"] : ["openai"];
}

function apiPath(model: CatalogModel, protocol: CatalogApiProtocol): string {
  if (protocol === "anthropic") return "messages";
  return model.mode === "embedding" ? "embeddings" : "chat/completions";
}

function apiPayload(model: CatalogModel, protocol: CatalogApiProtocol) {
  if (protocol === "anthropic") {
    return { model: model.id, max_tokens: 256, messages: [{ role: "user", content: "Hello" }] };
  }
  if (model.mode === "embedding") return { model: model.id, input: "Hello" };
  return { model: model.id, messages: [{ role: "user", content: "Hello" }] };
}

function apiMethod(model: CatalogModel, protocol: CatalogApiProtocol): string {
  if (protocol === "anthropic") return "messages.create";
  return model.mode === "embedding" ? "embeddings.create" : "chat.completions.create";
}

export function catalogApiExample(
  model: CatalogModel,
  apiBaseUrl: string,
  protocol: CatalogApiProtocol,
  language: CatalogApiLanguage,
): string {
  const base = apiBaseUrl.replace(/\/+$/, "");
  const apiBase = base.endsWith("/v1") ? base : `${base}/v1`;
  const modelValue = JSON.stringify(model.id);
  const anthropic = protocol === "anthropic";
  const embedding = model.mode === "embedding";
  const path = apiPath(model, protocol);
  const payload = apiPayload(model, protocol);
  const method = apiMethod(model, protocol);
  if (language === "curl") {
    const authorization = (
      anthropic
        ? ['  -H "x-api-key: $LITELLM_API_KEY" \\', '  -H "anthropic-version: 2023-06-01" \\']
        : ['  -H "Authorization: Bearer $LITELLM_API_KEY" \\']
    ).join("\n");
    return `curl ${JSON.stringify(`${apiBase}/${path}`)} \\
${authorization}
  -H "Content-Type: application/json" \\
  --data @- <<'JSON'
${JSON.stringify(payload, null, 2)}
JSON`;
  }
  if (language === "python") {
    const provider = anthropic ? "anthropic" : "openai";
    const client = anthropic ? "Anthropic" : "OpenAI";
    const input = embedding ? '    input="Hello",' : '    messages=[{"role": "user", "content": "Hello"}],';
    const maxTokens = anthropic ? "\n    max_tokens=256," : "";
    const endpoint = anthropic ? apiBase.replace(/\/v1$/, "") : apiBase;
    return `import os
from ${provider} import ${client}

client = ${client}(
    api_key=os.environ["LITELLM_API_KEY"],
    base_url=${JSON.stringify(endpoint)},
)

response = client.${method}(
    model=${modelValue},${maxTokens}
${input}
)
print(response)`;
  }
  const imported = anthropic ? 'import Anthropic from "@anthropic-ai/sdk";' : 'import OpenAI from "openai";';
  const className = anthropic ? "Anthropic" : "OpenAI";
  const endpoint = anthropic ? apiBase.replace(/\/v1$/, "") : apiBase;
  return `${imported}

const client = new ${className}({
  apiKey: process.env.LITELLM_API_KEY,
  baseURL: ${JSON.stringify(endpoint)},
});

const response = await client.${method}(${JSON.stringify(payload, null, 2)});
console.log(response);`;
}
