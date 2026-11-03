import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";
import { buildRequestExample, requestPath, type ApiProtocol, type ExampleLanguage } from "./requestExamples";

const protocols: readonly ApiProtocol[] = ["openai", "anthropic"];
const languages: readonly ExampleLanguage[] = ["curl", "python", "javascript"];
const baseUrl = "https://gateway.example/team-x/";

describe("landing request examples", () => {
  it("selects the endpoint for the chosen API protocol", () => {
    expect(requestPath("openai")).toBe("/v1/chat/completions");
    expect(requestPath("anthropic")).toBe("/v1/messages");
  });

  for (const protocol of protocols) {
    for (const language of languages) {
      it(`keeps the deployment prefix and credential placeholders in ${protocol} ${language}`, () => {
        const example = buildRequestExample(protocol, language, baseUrl);

        expect(example).toContain(`https://gateway.example/team-x${requestPath(protocol)}`);
        expect(example).toContain("YOUR_API_KEY");
        expect(example).toContain("YOUR_MODEL_ID");
        expect(example).toContain(protocol === "openai" ? "Authorization" : "x-api-key");
      });
    }

    it(`produces runnable JavaScript that sends the ${protocol} request`, async () => {
      const fetch = vi.fn(async (_url: string, _options: RequestInit) => ({
        ok: true,
        status: 200,
        json: async () => ({ message: "fixture" }),
      }));
      const example = buildRequestExample(protocol, "javascript", baseUrl);

      await runInNewContext(`(async () => { ${example} })()`, { fetch, console: { log: vi.fn() } });

      expect(fetch).toHaveBeenCalledTimes(1);
      const [url, options] = fetch.mock.calls[0];
      expect(url).toBe(`https://gateway.example/team-x${requestPath(protocol)}`);
      expect(options.method).toBe("POST");
      const headers = new Headers(options.headers);
      expect(headers.get("content-type")).toBe("application/json");
      expect(headers.get(protocol === "openai" ? "authorization" : "x-api-key")).toBe(
        protocol === "openai" ? "Bearer YOUR_API_KEY" : "YOUR_API_KEY",
      );
      const body = JSON.parse(String(options.body));
      expect(body.model).toBe("YOUR_MODEL_ID");
      expect(body.messages).toEqual([{ role: "user", content: "Hello, world." }]);
      if (protocol === "anthropic") {
        expect(headers.get("anthropic-version")).toBeTruthy();
        expect(body.max_tokens).toBeGreaterThan(0);
      }
    });
  }
});
