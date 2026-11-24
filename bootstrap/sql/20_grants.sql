-- Declarative grants per profile role (design §7.1). Lines whose principal is empty in the profile are skipped.
-- App service principals are granted by the `app_grants` bootstrap step (their ids exist only after `bundle deploy`).
-- ci
GRANT ALL PRIVILEGES ON SCHEMA $catalog.$schema TO `$sp_ci`;
GRANT USE CATALOG ON CATALOG $foreign_catalog TO `$sp_ingest`;
GRANT USE SCHEMA, SELECT ON SCHEMA $foreign_catalog.feeds TO `$sp_ingest`;
GRANT USE CATALOG ON CATALOG $catalog TO `$sp_ingest`;
GRANT USE SCHEMA, MODIFY, SELECT, CREATE TABLE ON SCHEMA $catalog.$schema TO `$sp_ingest`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME $catalog.$schema.raw_docs TO `$sp_ingest`;
-- wiki
GRANT USE CATALOG ON CATALOG $catalog TO `$sp_wiki`;
GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA $catalog.$schema TO `$sp_wiki`;
-- seed
GRANT USE CATALOG ON CATALOG $catalog TO `$sp_seed`;
GRANT ALL PRIVILEGES ON SCHEMA $catalog.$schema TO `$sp_seed`;
