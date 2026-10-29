"use client";

import { ArrowRight, BookOpen, Check, Circle, KeyRound, Layers, Route, ShieldCheck, Tags } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { uiHref } from "@/utils/uiHref";

interface GuideProgress {
  pools: number;
  boundPools: number;
  publishedPools: number;
  routingEnabled: boolean;
}

export default function PoolGuide({
  progress,
  onCreate,
  onActivate,
}: {
  progress: GuideProgress;
  onCreate: () => void;
  onActivate: () => void;
}) {
  const [step, setStep] = useState(0);
  const steps = [
    {
      title: "Define a resource pool",
      icon: Layers,
      complete: progress.pools > 0,
      description:
        "Give the resource pool a name your customers can understand. Choose its resource source and save a draft. You can add named channels after the pool exists, before publishing it.",
      action: "Create your first pool",
      run: onCreate,
    },
    {
      title: "Add named channels to the pool",
      icon: Route,
      complete: progress.boundPools > 0,
      description:
        "When adding an upstream in Models + Endpoints, enter a channel name and choose this resource pool. All models in that submission stay grouped under the channel. Use Channels to name existing deployments or move an entire channel between pools. The Provider field still chooses the HTTP protocol.",
      action: "Manage channels",
      href: uiHref("channels"),
    },
    {
      title: "Set access and model discounts",
      icon: Tags,
      complete: progress.publishedPools > 0,
      description:
        "Choose public or restricted access. A public pool can be inherited by accounts; restricted pools need explicit grants. Give this pool a default discount, then add customer or shared-plan model exceptions when needed. Pricing and permission are separate settings.",
      action: "Open customer pricing",
      href: uiHref("wallet-admin"),
    },
    {
      title: "Review routing for legacy keys",
      icon: ShieldCheck,
      complete: progress.routingEnabled,
      description:
        "Publish reviewed pools to make them available to explicitly configured resource-routing keys. Preview a route without calling a provider. Review model coverage and pricing before site-wide activation, which also applies pool routing to legacy keys.",
      action: "Review activation",
      run: onActivate,
    },
  ];
  const current = steps[step];
  const completed = steps.filter((item) => item.complete).length;
  return (
    <Card className="shadow-none">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <BookOpen className="size-4" />
            Getting started
          </CardTitle>
          <CardDescription className="mt-1">
            A guided setup for source-aware routing and negotiated pricing
          </CardDescription>
        </div>
        <Badge variant="outline">
          {completed} of {steps.length} setup checks complete
        </Badge>
      </CardHeader>
      <CardContent className="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
        <div className="flex flex-col gap-1">
          {steps.map((item, index) => (
            <button
              key={item.title}
              type="button"
              onClick={() => setStep(index)}
              aria-current={step === index ? "step" : undefined}
              className={`flex items-center gap-3 rounded-lg px-3 py-3 text-left text-sm transition-colors ${step === index ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted/60"}`}
            >
              {item.complete ? (
                <Check className="size-4 shrink-0 text-success" />
              ) : (
                <Circle className="size-4 shrink-0" />
              )}
              <span>
                {index + 1}. {item.title}
              </span>
            </button>
          ))}
        </div>
        <div className="rounded-xl border bg-muted/15 p-5 lg:p-6">
          <current.icon className="mb-4 size-6 text-muted-foreground" />
          <h3 className="text-base font-semibold">{current.title}</h3>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">{current.description}</p>
          <div className="mt-5 flex flex-wrap gap-2">
            {current.href ? (
              <Button render={<Link href={current.href} />}>
                {current.action}
                <ArrowRight />
              </Button>
            ) : (
              <Button onClick={current.run}>
                {current.action}
                <ArrowRight />
              </Button>
            )}
            {step < steps.length - 1 && (
              <Button variant="ghost" onClick={() => setStep(step + 1)}>
                Next step
              </Button>
            )}
          </div>
        </div>
        <div className="rounded-xl border p-4 lg:col-span-2">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <KeyRound className="size-4" />
            How API keys use resource pools
          </h3>
          <div className="mt-3 grid gap-4 text-sm md:grid-cols-3">
            <div>
              <p className="font-medium">Automatic</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                The key follows the account&apos;s permitted pool-model pairs. The gateway selects an eligible route for
                the requested model.
              </p>
            </div>
            <div>
              <p className="font-medium">Fixed pool</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                Pin the key to one permitted pool. It cannot switch to a different resource source.
              </p>
            </div>
            <div>
              <p className="font-medium">Custom pairs</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                Choose explicit pool-model pairs. Selecting a pool and a model elsewhere never creates additional
                combinations.
              </p>
            </div>
          </div>
          <Button className="mt-3 px-0" variant="link" render={<Link href={uiHref("api-keys")} />}>
            Manage virtual keys
            <ArrowRight />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
