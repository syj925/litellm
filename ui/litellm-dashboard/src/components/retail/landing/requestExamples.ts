export type ApiProtocol = "openai" | "anthropic";
export type ExampleLanguage = "curl" | "python" | "javascript";

const requests = {
  openai: {
    path: "/v1/chat/completions",
    headers: { Authorization: "Bearer YOUR_API_KEY", "Content-Type": "application/json" },
    body: { model: "YOUR_MODEL_ID", messages: [{ role: "user", content: "Hello, world." }] },
  },
  anthropic: {
    path: "/v1/messages",
    headers: {
      "x-api-key": "YOUR_API_KEY",
      "anthropic-version": "2023-06-01",
      "Content-Type": "application/json",
    },
    body: { model: "YOUR_MODEL_ID", max_tokens: 1024, messages: [{ role: "user", content: "Hello, world." }] },
  },
} as const;

export function requestPath(protocol: ApiProtocol): string {
  return requests[protocol].path;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

export function buildRequestExample(
  protocol: ApiProtocol,
  language: ExampleLanguage,
  baseUrl = "https://api.example.com",
): string {
  const request = requests[protocol];
  const url = `${baseUrl.replace(/\/+$/, "")}${request.path}`;
  const body = JSON.stringify(request.body, null, 2);
  const headers = JSON.stringify(request.headers, null, 2);
  if (language === "curl") {
    const continuation = String.fromCharCode(92);
    return [
      `curl ${shellQuote(url)} ${continuation}`,
      ...Object.entries(request.headers).map(
        ([key, value]) => `  -H ${shellQuote(`${key}: ${value}`)} ${continuation}`,
      ),
      `  -d ${shellQuote(body)}`,
    ].join("\n");
  }
  if (language === "python") {
    return [
      "import json",
      "import urllib.request",
      "",
      `payload = ${body}`,
      "",
      "request = urllib.request.Request(",
      `    ${JSON.stringify(url)},`,
      '    data=json.dumps(payload).encode("utf-8"),',
      `    headers=${headers.replace(/\n/g, "\n    ")},`,
      '    method="POST",',
      ")",
      "with urllib.request.urlopen(request) as response:",
      "    print(json.load(response))",
    ].join("\n");
  }
  return [
    "const response = await fetch(",
    `  ${JSON.stringify(url)},`,
    "  {",
    '    method: "POST",',
    `    headers: ${headers.replace(/\n/g, "\n    ")},`,
    `    body: JSON.stringify(${body.replace(/\n/g, "\n    ")}),`,
    "  }",
    ");",
    "",
    "if (!response.ok) {",
    "  throw new Error(`Request failed: ${response.status}`);",
    "}",
    "console.log(await response.json());",
  ].join("\n");
}
