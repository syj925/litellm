import type { Metadata } from "next";
import PublicModelCatalog from "@/components/retail/catalog/PublicModelCatalog";

export const metadata: Metadata = {
  title: "Pricing | AI Gateway",
  description: "Browse published model rates, compare token prices, and inspect cache pricing and usage conditions.",
};

export default function PricingPage() {
  return <PublicModelCatalog mode="pricing" />;
}
