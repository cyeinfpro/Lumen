export type RequestFence = {
  snapshot: () => number;
  advance: () => number;
  isCurrent: (snapshot: number) => boolean;
};

export function createRequestFence(): RequestFence {
  let revision = 0;
  return {
    snapshot: () => revision,
    advance: () => {
      revision += 1;
      return revision;
    },
    isCurrent: (snapshot) => snapshot === revision,
  };
}
