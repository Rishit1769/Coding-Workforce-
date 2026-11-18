import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV === "development";

// Production: static export served by the FastAPI control API (same origin, loopback only).
// Development: `next dev` proxies /api to the running control API.
const config: NextConfig = isDev
  ? {
      async rewrites() {
        return [{ source: "/api/:path*", destination: `http://127.0.0.1:${process.env.WORKFORCE_API_PORT ?? 8765}/api/:path*` }];
      },
    }
  : { output: "export", images: { unoptimized: true } };

export default config;
