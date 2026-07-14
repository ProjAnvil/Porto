import type { NextConfig } from "next";

// PORTO_STATIC_EXPORT=1 用于「捆绑部署」：产出纯静态文件，由后端 FastAPI 同源托管
// （无需 Node 运行时，也无需 rewrites 代理，前端直接用相对路径 /api/* 请求同源后端）。
const isStaticExport = process.env.PORTO_STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  ...(isStaticExport
    ? { output: "export" }
    : {
        async rewrites() {
          const backendUrl = process.env.PORTO_API_BASE_URL || "http://127.0.0.1:8100";
          return [
            {
              source: "/api/:path*",
              destination: `${backendUrl}/api/:path*`,
            },
          ];
        },
      }),
};

export default nextConfig;
