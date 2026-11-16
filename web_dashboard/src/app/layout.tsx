import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trillionaire | Quantitative Intraday Trading Cockpit",
  description:
    "Institutional Dark-Mode Algorithmic Execution Cockpit & Real-Time Telemetry Terminal for Zerodha Kite Connect",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-cockpit-bg text-cockpit-text min-h-screen antialiased selection:bg-cockpit-blue/30 selection:text-white">
        {children}
      </body>
    </html>
  );
}
