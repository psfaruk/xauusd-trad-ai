import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Gold AI Trader — XAUUSD AI Trading Platform",
  description:
    "Single-tenant XAUUSD (gold) trading analysis platform: live market feed, ICT/SMC chart analysis, SFP signal engine, practice trading planes and AI auto-trading.",
  keywords: ["XAUUSD", "gold trading", "AI trading", "SFP", "ICT", "SMC", "MT5"],
  icons: {
    icon: "https://z-cdn.chatglm.cn/z-ai/static/logo.svg",
  },
};

export const viewport: Viewport = {
  themeColor: "#09090b",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.variable} gold-app font-sans antialiased`}>
        {children}
        <Toaster />
      </body>
    </html>
  );
}
