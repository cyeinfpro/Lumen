import { ImageResponse } from "next/og";

export const size = { width: 512, height: 512 };
export const contentType = "image/png";

export default function Icon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background:
            "radial-gradient(circle at 50% 38%, #2a1d05 0%, #08080A 70%)",
          color: "#F5C04A",
          fontSize: 360,
          fontWeight: 600,
          // next/og (Satori) 不内置 serif 字体；fontFamily: "serif" 会 fallback
          // 到内置 Inter，渲染不可控。直接用 sans-serif + 加粗，得到稳定结果。
          fontFamily: "sans-serif",
          letterSpacing: -16,
          lineHeight: 1,
          // maskable purpose 要求安全区在中心 80%；字号已在中心，留白足够。
        }}
      >
        L
      </div>
    ),
    { ...size },
  );
}
