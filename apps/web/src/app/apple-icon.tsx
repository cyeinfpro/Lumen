import { ImageResponse } from "next/og";

export const size = { width: 180, height: 180 };
export const contentType = "image/png";

// iOS 不识别 maskable，圆角由系统裁，背景必须实心；不要透明。
export default function AppleIcon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#08080A",
          color: "#F5C04A",
          fontSize: 130,
          fontWeight: 600,
          // 同 icon.tsx：next/og 不内置 serif，强制 sans-serif 保证稳定渲染。
          fontFamily: "sans-serif",
          letterSpacing: -6,
          lineHeight: 1,
        }}
      >
        L
      </div>
    ),
    { ...size },
  );
}
