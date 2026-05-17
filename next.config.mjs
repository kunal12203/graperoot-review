const RAILWAY = "https://graperoot-review-production.up.railway.app";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async redirects() {
    return [
      { source: "/login", destination: `${RAILWAY}/login`, permanent: false },
    ];
  },
};

export default nextConfig;
