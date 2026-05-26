import base from "./next.config";

const nextConfig = {
  ...base,
  output: "standalone",
  images: { unoptimized: true },
};

export default nextConfig;
