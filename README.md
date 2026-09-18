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

## Install

```
pip install -r requirements.txt
```

## Configure

```
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set the referenced environment variables (secrets
are never stored in the YAML file itself, only `${VAR_NAME}` placeholders
resolved from the environment at load time).

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
  — Bucket Management* (v3.20.x). That guide does not document the
  authentication scheme (it defers to the *Storage Account Management API
  Developer Guide*); the tool supports both `basic` and `bearer` auth for
  COS — set `cos.auth.type` to match what your accesser actually expects.
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
