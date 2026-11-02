import { afterEach, describe, expect, it, vi } from "vitest";
import {
  attemptTransportIntervals,
  recordedIntervalMs,
  requestBodyIsAvailable,
  requestTraceDetailsCall,
  traceEventLabel,
  traceMilliseconds,
  traceTimestamp,
} from "./requestTrace";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
afterEach(() => vi.unstubAllGlobals());

describe("request trace evidence", () => {
  it("gives known request, authentication and pricing stages readable names", () => {
    expect(traceEventLabel("request.body_receive")).toBe("Read request body");
    expect(traceEventLabel("request.json_parse")).toBe("Parse request JSON");
    expect(traceEventLabel("auth /v1/messages")).toBe("Authentication · /v1/messages");
    expect(traceEventLabel("billing.prepare")).toBe("Prepare billing and freeze prices");
    expect(traceEventLabel("resource_pool.resolve")).toBe("Select resource pool");
  });

  it("does not label missing durations as zero and preserves the instant of timestamps with offsets", () => {
    expect(traceMilliseconds(null)).toBe("Not recorded");
    expect(traceMilliseconds(0)).toBe("0 ms");
    expect(recordedIntervalMs(5, 20)).toBe(15);
    expect(recordedIntervalMs(5, null)).toBeNull();
    expect(traceTimestamp("2026-11-02T08:30:00+08:00")).toBe("2026-11-02T00:30:00.000Z");
    expect(traceTimestamp("not a timestamp")).toBe("Not recorded");
  });

  it("only measures connection and transfer intervals from matching recorded event pairs", () => {
    const result = attemptTransportIntervals([
      { name: "connection.connect_tcp.started", at_ms: 1, attributes: {} },
      { name: "connection.connect_tcp.complete", at_ms: 4, attributes: {} },
      { name: "http11.send_request_body.started", at_ms: 5, attributes: {} },
      { name: "http11.send_request_body.complete", at_ms: 7, attributes: {} },
      { name: "response_body.started", at_ms: 12, attributes: {} },
    ]);
    expect(result.find((row) => row.label === "TCP connection")?.duration).toBe(3);
    expect(result.find((row) => row.label === "Send request body")?.duration).toBe(2);
    expect(result.find((row) => row.label === "TLS handshake")?.duration).toBeNull();
    expect(result.find((row) => row.label === "Read response body")?.duration).toBeNull();
  });

  it("hides expired or uncollected request bodies even when a stale payload contains a value", () => {
    const body = { available: true, expires_at: "2026-11-02T00:30:00Z", value: { content: "[redacted]" } };
    expect(requestBodyIsAvailable(body, Date.parse("2026-11-02T00:29:59Z"))).toBe(true);
    expect(requestBodyIsAvailable(body, Date.parse("2026-11-02T00:30:00Z"))).toBe(false);
    expect(requestBodyIsAvailable({ ...body, available: false }, 0)).toBe(false);
    expect(requestBodyIsAvailable({ ...body, value: null }, 0)).toBe(false);
  });

  it("encodes the log identifier and requests the protected trace without browser caching", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ recorded: false }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await requestTraceDetailsCall("admin-token", "call/with?query");
    expect(fetcher).toHaveBeenCalledOnce();
    const [url, options] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/retail/admin/request-traces/call%2Fwith%3Fquery");
    expect(new Headers(options.headers).get("Authorization")).toBe("Bearer admin-token");
    expect(options.cache).toBe("no-store");
  });
});
