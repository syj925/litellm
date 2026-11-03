"use client";

import { Suspense } from "react";
import ModelCatalogBrowser from "./ModelCatalogBrowser";
import { LoadingState } from "@/components/shared/LoadingState";
import { useAuth } from "@/contexts/AuthContext";
import PublicSiteShell from "../public-site/PublicSiteShell";
import styles from "./PublicModelCatalog.module.css";

export default function PublicModelCatalog({ mode = "models" }: { mode?: "models" | "pricing" }) {
  const { authLoading } = useAuth();
  return (
    <PublicSiteShell activePage={mode}>
      <main className={styles.page} data-auth-loading={authLoading}>
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
    </PublicSiteShell>
  );
}
