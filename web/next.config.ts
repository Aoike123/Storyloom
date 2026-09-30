import type { NextConfig } from 'next';

// The frontend proxies /api and /media to the backend. Default to the documented
// layout (8000); override with STORYLOOM_BACKEND_URL when the backend runs on a
// different port (e.g. 8010) so Storyloom can coexist with other local services.
const backend = process.env.STORYLOOM_BACKEND_URL ?? 'http://127.0.0.1:8000';

const config: NextConfig = {
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${backend}/api/:path*` },
      { source: '/media/:path*', destination: `${backend}/media/:path*` },
    ];
  },
};

export default config;
