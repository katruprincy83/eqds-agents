-- Lakehouse Federation to the external Azure SQL database (design §7.4). Credentials stay in the secret scope.
-- Rendered by job_env_bootstrap with the environment profile ($connection, $foreign_catalog, $azure_host, $azure_port, $azure_db, $secret_scope).
CREATE CONNECTION IF NOT EXISTS $connection TYPE sqlserver
OPTIONS (
  host '$azure_host',
  port '$azure_port',
  user secret('$secret_scope', 'azure-sql-user'),
  password secret('$secret_scope', 'azure-sql-password')
);
CREATE FOREIGN CATALOG IF NOT EXISTS $foreign_catalog USING CONNECTION $connection OPTIONS (database '$azure_db');
