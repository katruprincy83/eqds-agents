# Infrastructure that sits outside the bundle (design §4.2)

`scripts/infra.py` creates, idempotently and from `config/env/<target>.yaml`, everything the bundle cannot own:
the Unity Catalog catalog, the secret scope and its values (from `EQDS_AZURE_SQL_USER` / `EQDS_AZURE_SQL_PASSWORD`),
the Lakebase project, the Vector Search endpoint and service principals.

Where the platform team owns these through Terraform instead, keep the contract below and set `--skip` on `make infra`:

| Resource | Name from profile | Notes |
|---|---|---|
| `databricks_catalog` | `catalog` | managed location per cloud |
| `databricks_secret_scope` + secrets | `secret_scope` | keys `azure-sql-user`, `azure-sql-password` |
| Lakebase project / branch / database | `lakebase.*` | Autoscaling project |
| Vector Search endpoint | `vector_search_endpoint` | STANDARD |
| Service principals | `service_principals.*` | CI, bridge, ingest, wiki, seed |

No resource is defined here yet: `scripts/infra.py` is the source of truth until a Terraform module is requested.
