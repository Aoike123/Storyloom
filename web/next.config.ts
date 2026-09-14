import type { NextConfig } from 'next';
const config: NextConfig = {
  async rewrites() { return [{source:'/api/:path*',destination:'http://127.0.0.1:8000/api/:path*'}, {source:'/media/:path*',destination:'http://127.0.0.1:8000/media/:path*'}]; },
};
export default config;
