type Row = { label: string; value: string };

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function frameTimingRows(actual: Record<string, unknown>, prefix: string): Row[] {
  const raw = Array.isArray(actual.durations_ms) ? actual.durations_ms : [];
  const durations = raw.filter(positive);
  const complete = durations.length > 0 && durations.length === raw.length &&
    positive(actual.frame_count) && durations.length === actual.frame_count;
  if (!complete) return [
    { label: `${prefix}每轮时长`, value: "未知（帧延时缺失或为 0，不能将部分已知延时之和当作完整周期）" },
    { label: `${prefix}文件编码帧率`, value: "未知（帧延时缺失或为 0；由播放器决定，不使用请求时长推算）" },
  ];
  const rows: Row[] = [{
    label: `${prefix}每轮时长`,
    value: positive(actual.total_duration_ms) ? `${Number((actual.total_duration_ms / 1000).toFixed(3))} 秒` : "未知",
  }];
  if (durations.every(duration => duration === durations[0])) rows.push({
    label: `${prefix}文件编码帧率`,
    value: `${Number((1000 / durations[0]).toFixed(2))} FPS（${durations[0]} ms/帧；不保证播放器帧率）`,
  });
  else rows.push({ label: `${prefix}文件帧时长`, value: `可变：${Math.min(...durations)}–${Math.max(...durations)} ms` });
  return rows;
}

// Metadata describes decoded upstream bytes, not a sizing rule or playback guarantee.
export function gifNativeOutputRows(value: unknown): Row[] {
  const output = record(value);
  if (output?.output_format !== "gif" || !Array.isArray(output.actual)) return [];
  const actuals = output.actual;
  const rows: Row[] = [];
  const requested = record(output.requested);
  if (typeof requested?.size === "string") rows.push({ label: "请求每帧尺寸", value: requested.size });
  if (positive(requested?.frame_count)) rows.push({ label: "请求帧数", value: String(requested.frame_count) });
  if (positive(requested?.frame_duration_ms)) rows.push({ label: "请求每帧时长", value: `${requested.frame_duration_ms} ms` });
  actuals.slice(0, 10).forEach((value, index) => {
    const actual = record(value);
    if (!actual) return;
    const prefix = actuals.length > 1 ? `输出 ${index + 1} · ` : "";
    if (positive(actual.width) && positive(actual.height)) rows.push({ label: `${prefix}文件每帧尺寸`, value: `${actual.width}×${actual.height}` });
    if (positive(actual.frame_count)) rows.push({ label: `${prefix}实际帧数`, value: String(actual.frame_count) });
    rows.push(...frameTimingRows(actual, prefix));
    if (actual.loop === 0) rows.push({ label: `${prefix}循环`, value: "无限循环" });
    else if (positive(actual.loop) && Number.isInteger(actual.loop)) rows.push({ label: `${prefix}循环`, value: `有限循环（编码值 ${actual.loop}）` });
  });
  const labels: Record<string, string> = { size: "尺寸", frame_count: "帧数", frame_duration_ms: "每帧时长" };
  const differences = Array.isArray(output.mismatches) ? output.mismatches.filter((key): key is string => typeof key === "string" && Object.hasOwn(labels, key)) : [];
  if (differences.length) rows.push({ label: "与请求不同", value: `${differences.map((key) => labels[key]).join("、")}；以实际文件为准，不会自动重新生成` });
  return rows;
}
