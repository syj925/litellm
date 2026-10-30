"use client";

import Link from "next/link";
import { Suspense } from "react";
import { getProxyBaseUrl } from "@/components/networking";
import ThemeToggle from "@/components/ThemeToggle/ThemeToggle";
import { buttonVariants } from "@/components/ui/button";
import { uiHref } from "@/utils/uiHref";
import ModelCatalogBrowser from "./ModelCatalogBrowser";
import { LoadingState } from "@/components/shared/LoadingState";

export default function PublicModelCatalog() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-chrome border-b bg-background/95 backdrop-blur">
        <nav
          className="mx-auto flex h-14 max-w-[1600px] items-center gap-4 px-5 sm:px-8"
          aria-label="Public navigation"
        >
          <Link href={uiHref("models")} aria-label="LiteLLM models" className="mr-auto shrink-0">
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
          <Link href={uiHref("models")} aria-current="page" className="hidden text-sm font-medium sm:block">
            Models
          </Link>
          <a
            href="https://docs.litellm.ai/docs/"
            target="_blank"
            rel="noopener noreferrer"
            className="hidden text-sm text-muted-foreground hover:text-foreground sm:block"
          >
            Docs
          </a>
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
        <Suspense fallback={<LoadingState title="Loading model catalog" description="Loading published models." />}>
          <ModelCatalogBrowser publicView />
        </Suspense>
      </main>
      <footer className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3 border-t px-7 py-6 text-xs text-muted-foreground">
        <span>LiteLLM AI Gateway</span>
        <span>Explore the catalog. Sign in to manage your models and API keys.</span>
      </footer>
    </div>
  );
}
