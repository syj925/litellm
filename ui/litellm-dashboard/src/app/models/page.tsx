import type { Metadata } from "next";
import PublicModelCatalog from "@/components/retail/catalog/PublicModelCatalog";

export const metadata: Metadata = {
  title: "Models | LiteLLM",
  description: "Explore published AI models, compare capabilities and reference prices, and view integration examples.",
};

export default function ModelsPage() {
  return <PublicModelCatalog />;
}
