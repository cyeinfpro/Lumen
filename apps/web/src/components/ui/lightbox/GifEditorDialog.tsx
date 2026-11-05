"use client";
import { Button } from "@/components/ui/primitives/Button";
import { Select } from "@/components/ui/primitives/Select";
import { Dialog } from "@/components/ui/primitives/Dialog";
import { imageBinaryUrl } from "@/lib/apiClient";
import type { GifResult } from "@/lib/api/gifEditing";
import type { GifEditorRequest } from "./gifEditorEvents";
import { useGifEditor } from "./useGifEditor";

export function GifEditorDialog({ request, onClose, onContinue }: {
  request: GifEditorRequest; onClose: () => void; onContinue: (result: GifResult) => void;
}) {
  const editor = useGifEditor(request);
  const { info, busy, result } = editor;
  return (
    <Dialog open onClose={() => { if (!busy) onClose(); }} closeOnBackdrop={!busy} closeOnEscape={!busy}
      aria-label="GIF 本地编辑" aria-busy={busy} className="sm:w-[42rem]">
      <Dialog.Header><h2 className="type-card-title">GIF 本地编辑</h2></Dialog.Header>
      <Dialog.Body className="space-y-4">
        <p className="type-body-sm text-[var(--fg-1)]">在本地服务处理，不调用生成模型。保存为新版本，原图不变。</p>
        {editor.error ? <div role="alert" className="type-body-sm text-[var(--danger-fg)]">{editor.error}
          {!info ? <Button variant="secondary" onClick={editor.retry}>重试</Button> : null}</div> : null}
        {!info && !editor.error ? <p role="status">加载中</p> : null}
        {info ? <>
          <p className="type-caption text-[var(--fg-2)]">{info.width} × {info.height} · {info.frame_count} 帧 · {info.total_duration_ms} ms · {info.loop === null ? "播放一次" : info.loop === 0 ? "无限循环" : `循环 ${info.loop} 次`}</p>
          <SpeedControls editor={editor} />
          <FrameControls editor={editor} info={info} />
        </> : null}
        {result ? <section className="space-y-2" aria-label="新 GIF 版本">
          <p role="status" className="type-body-sm text-[var(--success-fg)]">已保存新版本，原图未修改。</p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={imageBinaryUrl(result.id)} alt="新 GIF 版本预览" className="max-h-64 max-w-full object-contain" />
          <a className="type-body-sm text-[var(--link-fg)] underline" href={imageBinaryUrl(result.id)} download>导出 GIF</a>
          <Button variant="secondary" onClick={() => onContinue(result)}>继续编辑</Button>
        </section> : null}
      </Dialog.Body>
      <Dialog.Footer><Button variant="secondary" disabled={busy} onClick={onClose}>{busy ? "保存中" : "关闭"}</Button></Dialog.Footer>
    </Dialog>
  );
}
function FramePreview({ src, label }: { src: string | null; label: string }) {
  return <figure className="min-w-0 space-y-1">
    <figcaption className="type-caption text-[var(--fg-2)]">{label}</figcaption>
    {src ? (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={src} alt={label} className="h-32 w-full rounded-[var(--radius-control)] border border-[var(--border)] object-contain" />
    ) : <div className="h-32 bg-[var(--bg-2)]" />}
  </figure>;
}

type Editor = ReturnType<typeof useGifEditor>;

function SpeedControls({ editor }: { editor: Editor }) {
  const { busy, result } = editor;
  return (
<fieldset disabled={busy || !!result} className="space-y-2 rounded-[var(--radius-panel)] border border-[var(--border)] p-3">
            <legend className="type-body-sm px-1">调整速度</legend>
            <label className="type-body-sm flex items-center gap-3">播放倍速
              <Select aria-label="播放倍速" value={editor.speed} onChange={(event) => editor.setSpeed(Number(event.target.value))}
                wrapperClassName="w-28 max-w-full">
                {[0.1, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 4, 10].map((value) => <option key={value} value={value}>{value}×</option>)}
              </Select>
            </label>
            <p className="type-caption text-[var(--fg-2)]">保留帧画面和循环设置，按比例调整时长。GIF 时长按最近 10 ms 舍入；零时长或超范围会明确报错。</p>
            <Button variant="secondary" disabled={busy || !!result || editor.speed === 1} onClick={() => void editor.save("speed")}>保存调速</Button>
          </fieldset>
  );
}

function FrameControls({ editor, info }: { editor: Editor; info: NonNullable<Editor["info"]> }) {
  const { busy, result } = editor;
  return (
<fieldset disabled={busy || !!result} className="space-y-3 rounded-[var(--radius-panel)] border border-[var(--border)] p-3">
            <legend className="type-body-sm px-1">替换单帧</legend>
            <label className="type-body-sm flex items-center gap-3">选择帧
              <Select aria-label="选择帧" value={editor.frame} onChange={(event) => editor.selectFrame(Number(event.target.value))}
                wrapperClassName="w-48 max-w-full">
                {info.durations_ms.map((duration, index) => <option key={index} value={index}>第 {index + 1} 帧 · {duration} ms</option>)}
              </Select>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <FramePreview src={editor.frameUrl} label="原始帧" />
              {editor.replacementUrl ? <FramePreview src={editor.replacementUrl} label="替换预览" /> : <p className="type-caption self-center text-[var(--fg-2)]">导出这一帧，在编辑器中修改后导入 PNG。</p>}
            </div>
            {editor.frameUrl ? <a className="type-body-sm text-[var(--link-fg)] underline" href={editor.frameUrl} download={`gif-frame-${editor.frame + 1}.png`}>导出 PNG</a> : <p className="type-caption">帧加载中</p>}
            <label className="type-body-sm block space-y-2"><span>导入替换 PNG</span>
              <input key={editor.frame} type="file" accept="image/png,.png" aria-label="导入替换 PNG"
                onChange={(event) => { void editor.chooseFile(event.target.files?.[0] ?? null); event.target.value = ""; }}
                className="block w-full min-w-0 type-caption" />
            </label>
            <p className="type-caption text-[var(--fg-2)]">尺寸须为 {info.width} × {info.height}，不超过 32 MiB。GIF 最多 256 色，替换帧可能量化颜色；半透明会按透明度 128 阈值转为全透明或不透明。其他帧、顺序、时长与循环保持不变。</p>
            {editor.file ? <p className="type-caption break-all">{editor.file.name}</p> : null}
            <Button variant="secondary" disabled={busy || !!result || !editor.file} onClick={() => void editor.save("frame")}>保存替换</Button>
          </fieldset>
  );
}
