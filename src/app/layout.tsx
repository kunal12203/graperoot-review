import type { Metadata } from "next";
import { Mulish, JetBrains_Mono } from "next/font/google";
import { Analytics } from "@vercel/analytics/next";
import SiteBackground from "@/components/SiteBackground";
import "./globals.css";

const mulish = Mulish({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "GrapeRoot Review — Graph-proven AI code review",
  icons: { icon: "/image.svg" },
  description:
    "AI code review that cites the real import chain. Every finding is graph-proven — not an LLM guess. Installs as a GitHub App in 2 minutes.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${mulish.variable} ${jetbrainsMono.variable}`}>
      <body className="font-mono">
        <SiteBackground />
        <div className="relative z-10">{children}</div>
        <Analytics />
      </body>
    </html>
  );
}
