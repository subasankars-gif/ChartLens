import type { NextConfig } from "next";

// Static export: the site is plain files on Firebase Hosting; all data comes from
// the ChartLens API on Cloud Run. No server-side rendering to host or secure.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
