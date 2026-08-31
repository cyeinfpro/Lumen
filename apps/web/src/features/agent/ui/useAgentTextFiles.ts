"use client";

import {
  type ChangeEvent,
  type Dispatch,
  type DragEvent,
  type MutableRefObject,
  type SetStateAction,
  useRef,
  useState,
} from "react";
import {
  AGENT_MAX_FILE_BYTES,
  AGENT_MAX_FILES,
  type AgentDraftFile,
} from "../model/contracts";

const TEXT_FILE_EXTENSIONS = new Set([
  "txt", "md", "csv", "json", "xml", "yaml", "yml", "js", "jsx", "ts",
  "tsx", "css", "html", "py", "sql", "sh", "toml", "ini", "log",
]);
const TEXT_APPLICATION_MIMES = new Set([
  "application/json",
  "application/xml",
  "application/yaml",
  "application/x-yaml",
  "application/javascript",
  "application/typescript",
  "application/sql",
]);

function extensionOf(name: string): string {
  return name.split(".").pop()?.toLocaleLowerCase() ?? "";
}

function isSupportedTextFile(file: File): boolean {
  return (
    file.type.startsWith("text/") ||
    TEXT_APPLICATION_MIMES.has(file.type) ||
    TEXT_FILE_EXTENSIONS.has(extensionOf(file.name))
  );
}

function mimeFromFileName(name: string): string {
  const extension = extensionOf(name);
  if (extension === "json") return "application/json";
  if (extension === "xml") return "application/xml";
  if (extension === "yaml" || extension === "yml") return "application/yaml";
  if (extension === "js" || extension === "jsx") return "application/javascript";
  if (extension === "ts" || extension === "tsx") return "application/typescript";
  if (extension === "sql") return "application/sql";
  return "text/plain";
}

async function readTextFile(file: File): Promise<AgentDraftFile> {
  if (file.size > AGENT_MAX_FILE_BYTES) {
    throw new Error(`${file.name} 超过 256 KB`);
  }
  if (!isSupportedTextFile(file)) {
    throw new Error(`${file.name} 不是支持的文本文件`);
  }
  const content = await file.text();
  if (content.includes("\u0000") || content.length > 200_000) {
    throw new Error(`${file.name} 不是有效的文本文件`);
  }
  return {
    name: file.name,
    mimeType: file.type || mimeFromFileName(file.name),
    size: file.size,
    content,
  };
}

export function useAgentTextFiles({
  currentCount,
  onAddFile,
  onError,
  dragDepthRef,
  setDragActive,
  onFallbackDrop,
}: {
  currentCount: number;
  onAddFile: (file: AgentDraftFile) => boolean;
  onError: (message: string | null) => void;
  dragDepthRef: MutableRefObject<number>;
  setDragActive: Dispatch<SetStateAction<boolean>>;
  onFallbackDrop: (event: DragEvent<HTMLDivElement>) => void | Promise<void>;
}) {
  const [isReadingFiles, setIsReadingFiles] = useState(false);
  const textFileInputRef = useRef<HTMLInputElement | null>(null);

  const addFiles = async (selected: File[]) => {
    if (selected.length === 0) return;
    if (currentCount + selected.length > AGENT_MAX_FILES) {
      onError(`每轮最多添加 ${AGENT_MAX_FILES} 个文本文件`);
      return;
    }
    setIsReadingFiles(true);
    try {
      for (const file of selected) {
        try {
          const added = onAddFile(await readTextFile(file));
          if (!added) onError(`${file.name} 已存在或文件总量已达上限`);
        } catch (error) {
          onError(error instanceof Error ? error.message : "文件读取失败");
        }
      }
    } finally {
      setIsReadingFiles(false);
    }
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const selected = Array.from(input.files ?? []);
    input.value = "";
    void addFiles(selected);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    const dropped = Array.from(event.dataTransfer.files ?? []);
    if (dropped.length === 0 || !dropped.every(isSupportedTextFile)) {
      void onFallbackDrop(event);
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = 0;
    setDragActive(false);
    void addFiles(dropped);
  };

  return {
    handleDrop,
    handleFileInput,
    isReadingFiles,
    textFileInputRef,
  };
}
