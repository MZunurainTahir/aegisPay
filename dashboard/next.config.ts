import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [
      {
        source: "/health",
        destination: "/api/health",
      },
      {
        source: "/stats",
        destination: "/api/stats",
      },
      {
        source: "/audit",
        destination: "/api/audit",
      },
      {
        source: "/audit/verify",
        destination: "/api/audit/verify",
      },
      {
        source: "/mandate",
        destination: "/api/mandate",
      },
      {
        source: "/kill",
        destination: "/api/kill",
      },
      {
        source: "/approve/:id",
        destination: "/api/approve/:id",
      },
      {
        source: "/act",
        destination: "/api/act",
      },
      {
        source: "/agent/run",
        destination: "/api/agent/run",
      },
      {
        source: "/invoices",
        destination: "/api/invoices",
      },
      {
        source: "/disputes",
        destination: "/api/disputes",
      },
    ];
  },
};

export default nextConfig;
