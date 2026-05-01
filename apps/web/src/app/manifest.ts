import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    // id 是 PWA 安装条目的稳定标识；变了浏览器会当成"新应用"重新装。
    // 一经发布就不要改。
    id: "/",
    name: "Lumen Studio",
    short_name: "Lumen",
    description: "A premium multimodal AI studio.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#08080A",
    theme_color: "#08080A",
    categories: ["productivity", "graphics"],
    lang: "zh-CN",
    dir: "ltr",
    // src 指向 app/icon.tsx 与 app/apple-icon.tsx 的 generated route：
    // 实际 URL 是 /icon 与 /apple-icon（无扩展名），由 Next 输出 PNG。
    icons: [
      {
        src: "/icon",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
      {
        src: "/apple-icon",
        sizes: "180x180",
        type: "image/png",
      },
    ],
  };
}
