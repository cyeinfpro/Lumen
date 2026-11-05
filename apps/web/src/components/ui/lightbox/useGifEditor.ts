"use client";
import { useEffect, useRef, useState } from "react";
import { extractGifFrame, inspectGif, replaceGifFrame, retimeGif, type GifInfo, type GifResult } from "@/lib/api/gifEditing";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import type { GifEditorRequest } from "./gifEditorEvents";

export function useGifEditor(request: GifEditorRequest) {
  const [info, setInfo] = useState<GifInfo | null>(null);
  const [error, setError] = useState("");
  const [frame, setFrame] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [frameImage, setFrameImage] = useState<{ index: number; url: string } | null>(null);
  const [replacementUrl, setReplacementUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GifResult | null>(null);
  const [reload, setReload] = useState(0);
  const running = useRef(false);
  const alive = useRef(true);
  const selection = useRef(0);
  const valid = () => alive.current && isPrivateIdentitySnapshotCurrent(request.identity);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; selection.current += 1; };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    inspectGif(request.item.id, controller.signal).then((value) => {
      if (!controller.signal.aborted && isPrivateIdentitySnapshotCurrent(request.identity)) {
        setInfo(value); setError("");
      }
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "读取 GIF 失败");
    });
    return () => controller.abort();
  }, [request, reload]);
  useEffect(() => {
    if (!info) return;
    const controller = new AbortController();
    let url: string | null = null;
    extractGifFrame(request.item.id, frame, controller.signal).then((blob) => {
      if (controller.signal.aborted || !isPrivateIdentitySnapshotCurrent(request.identity)) return;
      url = URL.createObjectURL(blob);
      setFrameImage({ index: frame, url });
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "读取帧失败");
    });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [request, frame, info, reload]);
  useEffect(() => () => { if (replacementUrl) URL.revokeObjectURL(replacementUrl); }, [replacementUrl]);
  const selectFrame = (index: number) => {
    selection.current += 1; setFrame(index); setFile(null); setReplacementUrl(null); setError("");
  };
  const chooseFile = async (candidate: File | null) => {
    const sequence = ++selection.current;
    setFile(null); setReplacementUrl(null); setError("");
    if (!candidate || !info) return;
    try {
      if (candidate.size > 32 * 1024 * 1024) throw new Error("PNG 不得超过 32 MiB");
      const header = new Uint8Array(await candidate.slice(0, 24).arrayBuffer());
      const signature = [137, 80, 78, 71, 13, 10, 26, 10];
      if (header.length !== 24 || signature.some((v, i) => header[i] !== v)) throw new Error("请选择有效的 PNG 文件");
      const view = new DataView(header.buffer);
      if (view.getUint32(16) !== info.width || view.getUint32(20) !== info.height) {
        throw new Error(`PNG 尺寸必须为 ${info.width} × ${info.height}`);
      }
      if (valid() && sequence === selection.current) { setFile(candidate); setReplacementUrl(URL.createObjectURL(candidate)); }
    } catch (reason) {
      if (valid() && sequence === selection.current) setError(reason instanceof Error ? reason.message : "无法读取 PNG");
    }
  };
  const save = async (mode: "speed" | "frame") => {
    if (running.current || result || !info || !valid() || (mode === "frame" && !file)) return;
    running.current = true; setBusy(true); setError("");
    try {
      const value = mode === "speed" ? await retimeGif(request.item.id, speed) :
        await replaceGifFrame(request.item.id, frame, file!);
      if (valid()) setResult(value);
    } catch (reason) {
      running.current = false;
      if (valid()) setError(reason instanceof Error ? reason.message : "保存失败，请重试；相同操作不会重复创建版本");
    } finally {
      if (valid()) setBusy(false);
    }
  };
  return { info, error, frame, speed, setSpeed, file, chooseFile, selectFrame,
    frameUrl: frameImage?.index === frame ? frameImage.url : null,
    replacementUrl: file ? replacementUrl : null, busy, result, save,
    retry: () => { setError(""); setReload((value) => value + 1); } };
}
