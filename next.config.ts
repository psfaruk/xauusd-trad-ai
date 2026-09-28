import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  /* config options here */
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,
  /* D-082 — engine.io polls "/socket.io/" (trailing slash): match the
   * app-route proxy as-is instead of 308-redirecting every long-poll. */
  skipTrailingSlashRedirect: true,
};

export default nextConfig;
