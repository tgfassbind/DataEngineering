# Object Storage

S3-compatible storage for the raw files. The fetcher writes one CSV per operating
day, the transform job reads them. Nothing else uses it.

Locally this is a SeaweedFS container (`chrislusf/seaweedfs:4.48`), started as
Compose service `object-storage`. It has no logic of its own: it only stores and
returns objects. All decisions (what to fetch, skip or overwrite) are made by the
fetcher.

## Contents of the bucket

```
s3://<S3_BUCKET>/            default: sbb-ist-daten
└── raw/
    ├── 2026-09-01_istdaten.csv
    ├── 2026-09-02_istdaten.csv
    └── …
```

- One object per operating day, original CSV byte for byte.
- The key does not show whether the file came from the permalink or the archive.
- `raw/` is the only prefix today. Further prefixes would be added next to it, never inside.

The full contract (completeness, overwrite rules) is described in the
[fetcher README](../fetcher/README.md#storage-contract).

## Operation

### Start and health

Started with the rest of the stack (`docker compose up -d`). The service is healthy
once `/healthz` answers; fetcher and transform wait for that before they start.

### Bucket

The storage service creates `S3_BUCKET` itself at startup. The fetcher does not
create it (`S3_CREATE_BUCKET=false`), it only waits up to `S3_BUCKET_WAIT_SECONDS`
for it to appear.

### Access from the host

Only the S3 API is published, on `127.0.0.1:8333` (`S3_API_PORT`), reachable from
this machine only. Any S3 client works with the credentials from `.env`, e.g.:

```bash
docker compose run --rm --entrypoint python fetcher -m app.inventory
```

### Data and reset

The files live in the Docker volume `deng-datapipeline_storage-data`. They survive
`docker compose down` and restarts.

> `docker compose down -v` or removing this volume deletes all raw files.
> Days that are no longer published at the source cannot be fetched again.

## Configuration

| Variable | Purpose |
|---|---|
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | credentials; the storage service uses them as its only user, fetcher and transform log in with them |
| `S3_BUCKET` | bucket created at startup |
| `S3_API_PORT` | host port of the S3 API (127.0.0.1 only) |
| `S3_ENDPOINT_URL` | how fetcher and transform reach the storage (`http://object-storage:8333` in the Docker network) |

## Design decisions

### Object storage instead of a folder

The raw files could have been written to a shared Docker volume. We chose object
storage on purpose, because the project will move to the cloud later. Fetcher and
transform talk to storage only through the S3 API, so the same code runs against
the local container and against AWS S3; only `.env` changes.

It also keeps the components decoupled: they share a bucket and a key convention,
not a file system mount.

### Storage without logic

The storage service only stores objects. Existence checks, overwrite rules and
validation all live in the fetcher. Replacing the storage (local container → AWS)
therefore changes nothing in the pipeline's behaviour.

### Local only, minimal surface

Only the S3 API is published, and only on `127.0.0.1`. All other SeaweedFS
interfaces (WebDAV, admin UI, Iceberg/Lance endpoints) are switched off. Fetcher
and transform reach the storage through the internal Docker network.

### Single node, no replication

SeaweedFS runs in single-node "mini" mode. There is no replication or backup:
if the volume is lost, the raw files are lost. This is acceptable for a local
setup. In the cloud, durability comes from the provider.

### Pinned version

The image is pinned to `chrislusf/seaweedfs:4.48`, so every team runs the same
storage version.