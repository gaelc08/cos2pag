# cos2pag

Sets up replication of a single IBM Cloud Object Storage (COS) bucket to a
PoINT Archival Gateway (PAG) repository, driven by PoINT Data Replicator
(PDR).

For a given bucket name, running the tool does:

1. **COS** (`<accesser>:8338/container/{bucket}`)
   - `GET` the bucket to read its current ACL and IP whitelist (`firewall`).
   - `PATCH` the bucket, adding the configured backup service account to
     the ACL and the PDR IP to the firewall whitelist (existing entries are
     preserved — ACL and firewall are re-sent in full, since the API
     replaces them wholesale on PATCH).
   - `PATCH` the bucket's `notifications.topic` to the bucket's own name.
2. **PAG** (`/api/partitions/{partitionUuid}/repositories`)
   - Create an object repository with the same name as the COS bucket in
     the configured partition, owned by the configured PDR user (or reuse
     it and update its owner if it already exists).
3. **PDR** (`/api/tasks`)
   - Create a replication task from the COS bucket (S3 source) to the PAG
     repository (S3 target, requires `sosapiEnabled` on the repository).

Every step is idempotent: re-running the tool on a bucket that is already
fully set up is a no-op (each step is reported as `SKIPPED`/`OK`).

## Prerequisites

- The PAG license must have **"Veeam SOSAPI" enabled** (check System >
  License in the PAG GUI). Without it, PAG repositories are never
  reachable over S3, so PDR can never replicate into them — this is a
  license limitation, not something this tool (or the PAG API) can work
  around. If it's off, get it turned on before setting up any bucket.
  Despite the name, it's the same generic `sosapiEnabled` / S3 API this
  tool already targets (port 4443) — it's just licensed under a
  "Veeam"-branded SKU because that's its most common use case, not
  because it's restricted to actual Veeam software. PDR (or any other S3
  client) works the same once it's on. The PAG admin API has no
  filesystem (NFS/CIFS) alternative to expose repositories, so this
  license is the only path to get PDR writing into PAG.
- Repositories created *before* SOSAPI was licensed can't be fixed by
  PATCHing `sosapiEnabled` after the fact either: PAG's license gates
  `ModifyObjectRepository()` as a whole (500 "product license does not
  cover this function"), separately from `CreateObjectRepository()`. A
  repository stuck like this needs to be recreated once the license is
  active, not patched.

## Install

```
pip install -r requirements.txt
```

## Configure

```
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml` for anything environment-specific (endpoints,
partition, owner, etc. — see below), and edit `.env` for secrets (never
committed, listed in `.gitignore`). `config.yaml`'s `${VAR_NAME}`
placeholders are resolved from the environment at load time, and
`python -m cos2pag` loads `.env` into the environment automatically
before that happens — no need to `export` anything by hand each time.
Use `--env-file <path>` if you keep secrets somewhere other than `.env`.

`config.example.yaml` is pre-filled with this environment's known
endpoints:

| System | Admin/Service API endpoint | S3 data-plane endpoint | Auth |
| --- | --- | --- | --- |
| COS | `osiris-109-a-fe.ctie.etat.lu:8338` | `s3.govcloud.etat.lu:443` | basic |
| PAG | `scrat-1.ctie.etat.lu:4200` | `scrat-1.ctie.etat.lu:4443` | basic |
| PDR | `hathor-1.ctie.etat.lu:3601` | n/a | basic |

The admin/service endpoints are used for bucket ACL/firewall/notifications
(COS) and repository/task management (PAG/PDR). The S3 data-plane
endpoints are what PDR actually connects to as `source_s3`/`target_s3` to
copy objects — they run on different ports than the admin APIs, so don't
mix them up.

## Run

```
python -m cos2pag my-bucket-name --config config.yaml
```

Add `--dry-run` to see what would be sent without making any mutating
request (`GET`s are still executed so the plan reflects real current
state), and `-v` for debug logging.

## Important safety note on the IP whitelist

If a bucket currently has **no** `allowed_ip` list at all, it is reachable
from any IP not explicitly denied. Sending a PATCH with
`firewall.allowed_ip: [<pdr ip>]` in that situation does not "add" an
entry — it **creates** a whitelist and immediately locks the bucket down
to that single IP. To avoid silently breaking existing access, the tool
refuses to do this by default and reports the `cos.firewall` step as
skipped with a warning. Set `cos.allow_create_whitelist: true` in the
config if that lock-down is actually what you want.

## Notes on API references used

- COS: *IBM Cloud Object Storage System — Container Mode Service API Guide
  — Bucket Management* (v3.20.x). Confirmed to use HTTP basic auth in this
  environment; the tool also supports `bearer` auth if that ever changes
  (set `cos.auth.type`).
- PAG: `pag-swagger.json` (PoINT Archival Gateway Administration API v1).
  A "bucket" is an Object Repository; the owner assigned to it is set via
  the `owner` field (`uuid` + `type`: `User`/`Group`/`ESP`).
- PDR: `pdr-swagger.json` (PoINT Data Replicator Administration API V1).
  A replication task's `source`/`target` are configured as S3 endpoints
  (`ApiTaskOptions.s3`).

## Tests

```
pytest
```
