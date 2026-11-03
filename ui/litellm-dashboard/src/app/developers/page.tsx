import type { Metadata } from "next";
import DevelopersGuide from "@/components/retail/landing/DevelopersGuide";

export const metadata: Metadata = {
  title: "Developers | AI Gateway",
  description:
    "Connect to the gateway, create an API key, choose a model, and copy request examples for supported API formats.",
};

export default function DevelopersPage() {
  return <DevelopersGuide />;
}
