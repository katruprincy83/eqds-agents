-- Declarative grants per profile role (design §7.1). Lines whose principal is empty in the profile are skipped.
-- App service principals are granted by the `app_grants` bootstrap step (their ids exist only after `bundle deploy`).
-- ci
GRANT ALL PRIVILEGES ON SCHEMA $catalog.$schema TO `$sp_ci`;
-- bridge worker: federated source + the UC tables the templates join
GRANT USE CATALOG ON CATALOG $foreign_catalog TO `$sp_bridge`;
GRANT USE SCHEMA, SELECT ON CATALOG $foreign_catalog TO `$sp_bridge`;
GRANT USE CATALOG ON CATALOG $catalog TO `$sp_bridge`;
GRANT USE SCHEMA ON SCHEMA $catalog.$schema TO `$sp_bridge`;
GRANT SELECT ON TABLE $catalog.$schema.efm_buy_basket TO `$sp_bridge`;
GRANT SELECT ON TABLE $catalog.$schema.prices_daily TO `$sp_bridge`;
GRANT SELECT ON TABLE $catalog.$schema.ref_companies TO `$sp_bridge`;
-- ingest
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
