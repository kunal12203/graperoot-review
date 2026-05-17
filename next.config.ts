import type { NextConfig } from "next";

const RAILWAY = "https://graperoot-review-production.up.railway.app";

const nextConfig: NextConfig = {
  async redirects() {
    return [
      { source: "/dashboard", destination: `${RAILWAY}/dashboard`, permanent: false },
      { source: "/login",     destination: `${RAILWAY}/login`,     permanent: false },
    ];
  },
};

export default nextConfig;
