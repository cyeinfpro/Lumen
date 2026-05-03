"use client";

// 圆环评分（0-100）。半径 18，stroke 3。颜色随分段变化。
// 分段：>=85 success / >=70 amber / >=50 warning / 其它 danger。

interface ScoreRingProps {
  score: number;
  size?: number;
  stroke?: number;
  showLabel?: boolean;
  className?: string;
}

const TIER = (score: number): string => {
  if (score >= 85) return "var(--success)";
  if (score >= 70) return "var(--amber-400)";
  if (score >= 50) return "var(--warning)";
  return "var(--danger)";
};

export function ScoreRing({
  score,
  size = 40,
  stroke = 3,
  showLabel = true,
  className,
}: ScoreRingProps) {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, score));
  const dash = (clamped / 100) * circumference;
  const color = TIER(clamped);

  return (
    <div className={className} style={{ width: size, height: size, position: "relative" }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--border)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dasharray 320ms cubic-bezier(0.22, 1, 0.36, 1)" }}
        />
      </svg>
      {showLabel ? (
        <span
          className="absolute inset-0 flex items-center justify-center text-[10px] font-medium tabular-nums"
          style={{ color }}
        >
          {Math.round(clamped)}
        </span>
      ) : null}
    </div>
  );
}
