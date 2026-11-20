import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // Leave time for the backend to return its own timeout (Ollama budget up to 600 s).
  experimental: { proxyTimeout: 650_000 },
  async rewrites() {
    return [{ source: "/api/:path*", destination: "http://backend:8000/:path*" }];
  },
};

export default config;
