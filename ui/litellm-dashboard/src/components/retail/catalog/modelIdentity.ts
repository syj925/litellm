export interface ModelIdentity {
  displayName: string;
  author: string;
  providerSlug: string;
}

const authors: Readonly<Record<string, { author: string; providerSlug: string }>> = {
  anthropic: { author: "Anthropic", providerSlug: "anthropic" },
  openai: { author: "OpenAI", providerSlug: "openai" },
  google: { author: "Google", providerSlug: "gemini" },
  gemini: { author: "Google", providerSlug: "gemini" },
  meta: { author: "Meta", providerSlug: "meta_llama" },
  deepseek: { author: "DeepSeek", providerSlug: "deepseek" },
  mistral: { author: "Mistral AI", providerSlug: "mistral" },
  mistralai: { author: "Mistral AI", providerSlug: "mistral" },
  qwen: { author: "Qwen", providerSlug: "qwen_ai_platform" },
  xai: { author: "xAI", providerSlug: "xai" },
  moonshot: { author: "Moonshot AI", providerSlug: "moonshot" },
  moonshotai: { author: "Moonshot AI", providerSlug: "moonshot" },
};

const families = [
  { pattern: /^claude-/i, author: "anthropic", prefix: "Claude " },
  { pattern: /^gpt-/i, author: "openai", prefix: "GPT-" },
  { pattern: /^gemini-/i, author: "google", prefix: "Gemini " },
  { pattern: /^llama(?:-|(?=\d)|$)/i, author: "meta", prefix: "Llama " },
  { pattern: /^deepseek(?:-|$)/i, author: "deepseek", prefix: "DeepSeek " },
  { pattern: /^mistral(?:-|$)/i, author: "mistral", prefix: "Mistral " },
  { pattern: /^qwen(?:-|(?=\d)|$)/i, author: "qwen", prefix: "Qwen " },
  { pattern: /^grok(?:-|$)/i, author: "xai", prefix: "Grok " },
  { pattern: /^kimi(?:-|$)/i, author: "moonshot", prefix: "Kimi " },
] as const;

const wordCase: Readonly<Record<string, string>> = { oss: "OSS", api: "API", xl: "XL", a: "A" };

function displayWords(value: string): string {
  return value
    .replace(/-(?:19|20)\d{6}(?=-|$)/g, "")
    .replace(/(\d)-(?=\d(?:-|$))/g, "$1.")
    .split("-")
    .filter(Boolean)
    .map((word) => wordCase[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function getModelIdentity(id: string, author?: string | null): ModelIdentity {
  const name = id.split("/").at(-1) ?? id;
  const family = families.find(({ pattern }) => pattern.test(name));
  const explicitAuthor = author?.trim();
  const authorIdentity = authors[(explicitAuthor ?? "").toLowerCase().replace(/[\s_-]/g, "")];
  const identity = authorIdentity ?? (family ? authors[family.author] : undefined);
  const openaiReasoning = /^o\d(?:[\w.-]*$)/i.test(name);
  if (family) {
    return { ...identity!, displayName: family.prefix + displayWords(name.replace(family.pattern, "")) };
  }
  if (openaiReasoning) {
    return { ...(identity ?? authors.openai), displayName: name.replace(/-/g, " ") };
  }
  return {
    displayName: id,
    author: identity?.author ?? (explicitAuthor || "Unknown author"),
    providerSlug: identity?.providerSlug ?? "",
  };
}

export function formatContext(tokens: number | null | undefined): string {
  if (tokens == null || !Number.isFinite(tokens) || tokens <= 0) return "Not specified";
  const unit = tokens >= 1_000_000 ? { divisor: 1_000_000, suffix: "M" } : { divisor: 1_000, suffix: "K" };
  if (tokens < 1_000) return new Intl.NumberFormat("en-US").format(tokens);
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(tokens / unit.divisor) + unit.suffix;
}
