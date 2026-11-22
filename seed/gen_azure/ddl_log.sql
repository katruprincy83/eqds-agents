-- Idempotency log used only by the serverless batch-insert path (dropped at the end of job_seed_azure).
IF OBJECT_ID('oms._seed_batches') IS NULL CREATE TABLE oms._seed_batches (batch_id varchar(80) NOT NULL PRIMARY KEY);
GO
