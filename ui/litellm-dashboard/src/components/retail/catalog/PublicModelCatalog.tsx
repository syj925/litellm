"use client";

import Link from "next/link";
import { Suspense } from "react";
import { getProxyBaseUrl } from "@/components/networking";
import ThemeToggle from "@/components/ThemeToggle/ThemeToggle";
import { buttonVariants } from "@/components/ui/button";
import { uiHref } from "@/utils/uiHref";
import ModelCatalogBrowser from "./ModelCatalogBrowser";
import { LoadingState } from "@/components/shared/LoadingState";
import { useAuth } from "@/contexts/AuthContext";

export default function PublicModelCatalog({ mode = "models" }: { mode?: "models" | "pricing" }) {
  const { authLoading } = useAuth();
  return (
    <div className="min-h-screen bg-background text-foreground" data-auth-loading={authLoading}>
      <header className="sticky top-0 z-chrome border-b bg-background/95 backdrop-blur">
        <nav
          className="mx-auto flex min-h-14 max-w-[1600px] flex-wrap items-center gap-4 px-5 py-2 sm:px-8"
          aria-label="Public navigation"
        >
          <Link href={uiHref("welcome/")} aria-label="AI Gateway home" className="mr-auto shrink-0">
            <img
              src={`${getProxyBaseUrl()}/get_image`}
              alt="LiteLLM"
              className="h-6 w-auto max-w-36 object-contain dark:hidden"
            />
            <img
              src={`${getProxyBaseUrl()}/get_image?theme=dark`}
              alt="LiteLLM"
              className="hidden h-6 w-auto max-w-36 object-contain dark:block"
            />
          </Link>
          <div className="order-last flex w-full items-center gap-5 border-t pt-2 text-sm sm:order-none sm:w-auto sm:border-0 sm:pt-0">
            <Link
              href={uiHref("models/")}
              aria-current={mode === "models" ? "page" : undefined}
              className="flex min-h-11 items-center font-medium hover:underline"
            >
              Models
            </Link>
            <Link href={uiHref("developers/")} className="flex min-h-11 items-center font-medium hover:underline">
              Developers
            </Link>
            <Link
              href={uiHref("pricing/")}
              aria-current={mode === "pricing" ? "page" : undefined}
              className="flex min-h-11 items-center font-medium hover:underline"
            >
              Pricing
            </Link>
          </div>
          <ThemeToggle />
          <Link href={uiHref("login")} className={buttonVariants({ variant: "outline", size: "sm" })}>
            Sign in
          </Link>
          <Link href={uiHref("signup")} className={buttonVariants({ size: "sm", className: "hidden sm:inline-flex" })}>
            Get started
          </Link>
        </nav>
      </header>
      <main className="mx-auto max-w-[1600px]">
        <Suspense
          fallback={
            <LoadingState
              title={mode === "pricing" ? "Loading pricing" : "Loading model catalog"}
              description="Loading published models and reference prices."
            />
          }
        >
          <ModelCatalogBrowser publicView publicMode={mode} />
        </Suspense>
      </main>
      <footer className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3 border-t px-7 py-6 text-xs text-muted-foreground">
        <span>LiteLLM AI Gateway</span>
        <span>Explore the catalog. Sign in to manage your models and API keys.</span>
      </footer>
    </div>
  );
}
