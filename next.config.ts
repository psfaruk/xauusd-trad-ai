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
  /* D-083 — cloud-deploy (Railway) build memory: Turbopack peaks ~1.1GB RSS
   * on this dependency graph, which OOM-kills the build on small plans
   * (512MB trial). Webpack + memory optimizations + capped build workers
   * cut the peak to well under that (measured locally). */
  experimental: {
    webpackMemoryOptimizations: true,
    cpus: 2,
  },
};

export default nextConfig;
