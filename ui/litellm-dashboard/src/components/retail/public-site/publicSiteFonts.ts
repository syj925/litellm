import localFont from "next/font/local";

const geist = localFont({
  src: "../../../../public/retail-landing/fonts/geist-latin-wght-normal.woff2",
  weight: "100 900",
  variable: "--font-gateway-geist",
  display: "swap",
});
const mono = localFont({
  src: "../../../../public/retail-landing/fonts/roboto-mono-latin-wght-normal.woff2",
  weight: "100 700",
  variable: "--font-gateway-mono",
  display: "swap",
});

export const publicSiteFontClassName = `${geist.variable} ${mono.variable}`;
