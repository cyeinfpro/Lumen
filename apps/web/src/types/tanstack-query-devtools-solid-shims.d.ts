// @tanstack/react-query-devtools depends on @tanstack/query-devtools, whose
// declarations reference optional Solid packages. The web app does not ship
// Solid; this keeps `tsc --skipLibCheck false` type-only.
declare module "solid-js" {
  export type Accessor<T = unknown> = () => T;
  export type Component<P = Record<string, never>> = (props: P) => JSX.Element;
  export type Context<T = unknown> = {
    Provider?: unknown;
    defaultValue?: T;
  };

  export interface JSX {
    readonly __solidJsxBrand?: never;
  }

  export namespace JSX {
    export type Element = unknown;
  }
}

declare module "@solid-primitives/storage" {
  export type StorageObject<T = unknown> = Record<string, T>;
  export type StorageSetter<T = unknown, V = unknown> = (key: string, value: V) => T;
}
