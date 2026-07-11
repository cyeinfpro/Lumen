# image-job

sub2api image async job sidecar for <CALLER_APP>.

Full deployment and integration guide:

```text
image-job.md
```

Example production layout:

```text
app root: /opt/image-job
data dir: /opt/image-job/data
db path:  /var/lib/image-job/state/image_jobs.sqlite3
public:   https://example.com/images/temp/
```

The service forwards image requests to local sub2api:

```text
IMAGE_JOB_UPSTREAM_BASE_URL=http://127.0.0.1:8081
```

Nginx exposes generated temporary images by aliasing:

```text
/images/temp/ -> /opt/image-job/data/images/temp/
```

Public API:

```text
POST /v1/image-jobs
GET  /v1/image-jobs/{job_id}
POST /v1/refs
GET  /images/temp/...
GET  /refs/...
```

The caller must send the same `Authorization: Bearer <UPSTREAM_API_KEY>` when creating and polling a job.

Each upstream POST carries a stable `Idempotency-Key` derived from the persisted
`job_id`. The service does not assume that an upstream honors this key unless
`IMAGE_JOB_UPSTREAM_IDEMPOTENCY_GUARANTEED=1` is configured. Ambiguous read,
gateway, restart, or stream failures are otherwise terminal `uncertain` jobs and
are not automatically replayed.

Request JSON, non-stream upstream bodies, individual images, aggregate image
bytes, and candidate counts all have configurable hard limits. See
`.env.example` and `image-job.md`.
