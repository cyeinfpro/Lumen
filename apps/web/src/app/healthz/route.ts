export const dynamic = "force-static";

const HEALTH_HEADERS = {
  "Cache-Control": "no-store",
};

export function GET() {
  return Response.json(
    {
      status: "ok",
      service: "web",
    },
    {
      headers: HEALTH_HEADERS,
    },
  );
}

export function HEAD() {
  return new Response(null, {
    status: 204,
    headers: HEALTH_HEADERS,
  });
}
