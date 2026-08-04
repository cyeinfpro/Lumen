type IdempotentBody = {
  idempotency_key: string;
};

type SemanticPostInit = Omit<RequestInit, "method">;
type SemanticJsonPostInit = Omit<SemanticPostInit, "body">;

export function semanticPostRequest(
  idempotencyKey: string,
  init: SemanticPostInit = {},
): RequestInit {
  const headers = new Headers(init.headers);
  const existing = headers.get("Idempotency-Key");
  if (existing !== null && existing !== idempotencyKey) {
    throw new TypeError("Idempotency-Key header must match semantic key");
  }
  headers.set("Idempotency-Key", idempotencyKey);
  return {
    ...init,
    method: "POST",
    headers,
  };
}

export function semanticJsonPostRequest<TBody>(
  body: TBody,
  idempotencyKey: string,
  init: SemanticJsonPostInit = {},
): RequestInit {
  return semanticPostRequest(idempotencyKey, {
    ...init,
    body: JSON.stringify(body),
  });
}

export function idempotentPostRequest<TBody extends IdempotentBody>(
  body: TBody,
  init: SemanticJsonPostInit = {},
): RequestInit {
  return semanticJsonPostRequest(body, body.idempotency_key, init);
}
