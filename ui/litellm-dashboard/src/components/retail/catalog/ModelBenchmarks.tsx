"use client";

import { useState } from "react";
import { ArrowUpRight, BarChart3 } from "lucide-react";
import { BarChart } from "@/components/shared/charts";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { catalogModelTitle } from "./modelIdentity";
import { metadataDate } from "./MetadataSources";
import type { BenchmarkRecord, CatalogModel } from "./types";

interface Observation {
  model: CatalogModel;
  benchmark: BenchmarkRecord;
}

const sourceLabels = { models_dev: "models.dev", openrouter: "OpenRouter" } as const;

export function benchmarkProtocol(benchmark: BenchmarkRecord): string {
  return JSON.stringify([
    benchmark.source,
    benchmark.name,
    benchmark.metric,
    benchmark.version,
    benchmark.harness,
    benchmark.dataset,
    benchmark.variant,
  ]);
}

function label(benchmark: BenchmarkRecord): string {
  return [benchmark.name, benchmark.version, benchmark.metric, benchmark.variant, sourceLabels[benchmark.source]]
    .filter(Boolean)
    .join(" · ");
}

function score(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value);
}

function safeCitation(value: string): string | null {
  try {
    const parsed = new URL(value);
    return ["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? value : null;
  } catch {
    return null;
  }
}

function observations(models: CatalogModel[]): Observation[] {
  return models.flatMap((model) =>
    (model.benchmarks ?? [])
      .filter((benchmark) => Number.isFinite(benchmark.score))
      .map((benchmark) => ({ model, benchmark })),
  );
}

export default function ModelBenchmarks({ models }: { models: CatalogModel[] }) {
  const [selectedProtocol, setSelectedProtocol] = useState("");
  const entries = observations(models);
  const protocols = [...new Set(entries.map(({ benchmark }) => benchmarkProtocol(benchmark)))];
  const groups = protocols
    .map((key) => ({
      key,
      entries: entries.filter(({ benchmark }) => benchmarkProtocol(benchmark) === key),
    }))
    .sort((left, right) => new Set(right.entries.map((entry) => entry.model.id)).size - new Set(left.entries.map((entry) => entry.model.id)).size);
  const selected = groups.find(({ key }) => key === selectedProtocol) ?? groups[0];
  if (!selected) {
    return (
      <section aria-label="Model benchmark results" className="rounded-lg border p-6">
        <h2 className="flex items-center gap-2 font-semibold">
          <BarChart3 className="size-4" /> Benchmark results
        </h2>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          No sourced benchmark scores have been published for this exact model. Capability tags are not converted into
          scores.
        </p>
      </section>
    );
  }
  const benchmark = selected.entries[0].benchmark;
  const data = selected.entries.map(({ model, benchmark: result }, index) => ({
    label:
      selected.entries.filter((entry) => entry.model.id === model.id).length > 1
        ? `${catalogModelTitle(model)} (${index + 1})`
        : catalogModelTitle(model),
    Score: result.score,
  }));
  return (
    <section aria-label="Model benchmark results" className="space-y-5 pt-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <BarChart3 className="size-5" /> Benchmark results
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
            Source-reported model evaluations, separate from this gateway&apos;s operational performance. Scores keep
            their original units.
          </p>
        </div>
        <Select
          value={selected.key}
          items={groups.map((group) => ({ value: group.key, label: label(group.entries[0].benchmark) }))}
          onValueChange={(value) => value && setSelectedProtocol(value)}
        >
          <SelectTrigger aria-label="Select benchmark" className="w-full min-w-0 sm:max-w-lg">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {groups.map((group) => (
              <SelectItem key={group.key} value={group.key}>
                {label(group.entries[0].benchmark)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="rounded-lg border p-4">
        <h3 className="text-sm font-semibold">{benchmark.name}</h3>
        <p className="mt-1 text-xs leading-6 text-muted-foreground">
          {[
            benchmark.metric ? `Metric: ${benchmark.metric}` : "Metric not supplied",
            benchmark.version ? `Version: ${benchmark.version}` : "Version not supplied",
            benchmark.harness && `Harness: ${benchmark.harness}`,
            benchmark.dataset && `Dataset: ${benchmark.dataset}`,
            benchmark.variant && `Setting: ${benchmark.variant}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
        <p className="text-xs leading-6 text-muted-foreground">
          {new Set(selected.entries.map((entry) => entry.model.id)).size} of {models.length} models have a published result for this protocol.
        </p>
        <div aria-label={`${benchmark.name} raw scores`} style={{ height: Math.max(256, data.length * 34 + 48) }}>
          {benchmark.metric ? (
            <BarChart
              data={data}
              index="label"
              categories={["Score"]}
              layout="vertical"
              yAxisWidth={170}
              colors={["blue"]}
              valueFormatter={score}
              showLegend={false}
              maxBarSize={32}
              className="h-full"
            />
          ) : (
            <p className="flex h-64 items-center justify-center px-5 text-center text-sm text-muted-foreground">
              The source has not supplied score units. Raw observations remain listed below.
            </p>
          )}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          Raw scores are not a percentage of overall ability. Different benchmark versions, settings, datasets and
          sources are shown separately. Missing protocol details limit comparability.
        </p>
      </div>
      <div className="overflow-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead>Score</TableHead>
              <TableHead>Source rank</TableHead>
              <TableHead>Evaluation date</TableHead>
              <TableHead>Source</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {selected.entries.map(({ model, benchmark: result }, index) => {
              const citation = safeCitation(result.source_url);
              return (
                <TableRow key={`${model.id}-${index}`}>
                  <TableCell>
                    <span className="font-medium">{catalogModelTitle(model)}</span>
                    <code className="mt-1 block text-xs text-muted-foreground">{model.id}</code>
                  </TableCell>
                  <TableCell className="font-medium tabular-nums">
                    {score(result.score)}
                    {result.metric && (
                      <span className="ml-1 text-xs font-normal text-muted-foreground">{result.metric}</span>
                    )}
                  </TableCell>
                  <TableCell>{result.rank != null ? `#${result.rank}` : "Not supplied"}</TableCell>
                  <TableCell>{result.evaluated_at || "Not supplied"}</TableCell>
                  <TableCell>
                    {citation ? (
                      <a
                        href={citation}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-xs hover:underline"
                      >
                        {sourceLabels[result.source]} <ArrowUpRight className="size-3" />
                      </a>
                    ) : (
                      sourceLabels[result.source]
                    )}
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      Synced {metadataDate(result.retrieved_at)} UTC
                    </p>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
