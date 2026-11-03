"use client";

import { publicSiteThemeClassName } from "../public-site/PublicSiteShell";
import { useState } from "react";
import { BarChart3 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import ModelBenchmarks from "./ModelBenchmarks";
import type { CatalogModel } from "./types";

export default function ModelBenchmarkExplorer({
  models,
  publicView = false,
}: {
  models: CatalogModel[];
  publicView?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const hasScores = models.some((model) => model.benchmarks?.some((benchmark) => Number.isFinite(benchmark.score)));
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)} disabled={!hasScores}>
        <BarChart3 /> Benchmarks
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          className={[
            "max-h-[calc(100dvh-2rem)] min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden sm:w-[calc(100vw-3rem)] sm:max-w-6xl",
            publicView ? publicSiteThemeClassName : "",
          ].join(" ")}
        >
          <DialogHeader className="pr-8">
            <DialogTitle>Model benchmarks</DialogTitle>
            <DialogDescription>
              Explore evaluations for the {models.length} {models.length === 1 ? "model" : "models"} currently shown in
              this catalog. Sources and test settings remain separate.
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 min-w-0 max-h-[70dvh] overflow-auto pr-1">
            <ModelBenchmarks models={models} publicView={publicView} />
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
