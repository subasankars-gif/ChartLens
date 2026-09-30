import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ChartLens",
  description: "See the structure. Read the trend.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-dvh">{children}</body>
    </html>
  );
}
