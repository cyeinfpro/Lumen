"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useState,
} from "react";
import { useStore } from "zustand";

import {
  createCanvasEditorStore,
  type CanvasEditorState,
  type CanvasEditorStore,
} from "@/lib/canvas/store";
import type { CanvasGraph } from "@/lib/canvas/types";

const CanvasStoreContext = createContext<CanvasEditorStore | null>(null);

export function CanvasStoreProvider({
  graph,
  revision,
  children,
}: {
  graph: CanvasGraph;
  revision: number;
  children: ReactNode;
}) {
  const [store] = useState(() => createCanvasEditorStore(graph, revision));
  return (
    <CanvasStoreContext.Provider value={store}>
      {children}
    </CanvasStoreContext.Provider>
  );
}

export function useCanvasStore<T>(selector: (state: CanvasEditorState) => T): T {
  const store = useCanvasStoreApi();
  return useStore(store, selector);
}

export function useCanvasStoreApi(): CanvasEditorStore {
  const store = useContext(CanvasStoreContext);
  if (!store) throw new Error("CanvasStoreProvider is missing");
  return store;
}
