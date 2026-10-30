import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CacheUsageBreakdown from "./CacheUsageBreakdown";

describe("cache usage breakdown", () => {
  it("shows separate known and unspecified durations without disguising the total as an extra bucket", () => {
    render(<CacheUsageBreakdown usage={{ cache_read_tokens: 4, cache_write_tokens: 100,
      cache_write_ttl_tokens: { "300": 20, "3600": 30, "43200": 40 }, cache_write_unclassified_tokens: 10 }} />);
    expect(screen.getByText("Cache write tokens · total")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 5m")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 1h")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 12h")).toBeInTheDocument();
    expect(screen.getByText("Cache write · unspecified duration")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });
});
