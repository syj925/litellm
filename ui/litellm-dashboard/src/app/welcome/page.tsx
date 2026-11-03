import type { Metadata } from "next";
import GatewayLanding from "@/components/retail/landing/GatewayLanding";

export const metadata: Metadata = {
  title: "AI Gateway | One connection. More possibilities.",
  description: "Discover models, connect through familiar APIs, and follow your usage in one account.",
};

export default function WelcomePage() {
  return <GatewayLanding />;
}
