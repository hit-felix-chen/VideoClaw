import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: '/code/:path*',
        destination: 'http://127.0.0.1:8000/code/:path*',
      },
      {
        source: '/api/library/:path*',
        destination: 'http://127.0.0.1:8000/api/library/:path*',
      },
      {
        source: '/api/v2/:path*',
        destination: 'http://127.0.0.1:8000/api/v2/:path*',
      },
      {
        source: '/api/upload_media',
        destination: 'http://127.0.0.1:8000/api/upload_media',
      },
      {
        source: '/api/models',
        destination: 'http://127.0.0.1:8000/api/models',
      },
      {
        source: '/api/config',
        destination: 'http://127.0.0.1:8000/api/config',
      },
      {
        source: '/api/cache/:path*',
        destination: 'http://127.0.0.1:8000/api/cache/:path*',
      },
      // 临时工作台 API
      {
        source: '/api/sandbox/:path*',
        destination: 'http://127.0.0.1:8000/api/sandbox/:path*',
      },
    ];
  },
};

export default nextConfig;
