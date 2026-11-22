-- SYNTHETIC DEMO DATA. Azure SQL Database schema for the external OMS / research / feeds (design §7.4).
-- Idempotent: objects are created only if absent. Executed by job_seed_azure (classic) over JDBC, batch-split on GO.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'oms') EXEC('CREATE SCHEMA oms');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'research') EXEC('CREATE SCHEMA research');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'feeds') EXEC('CREATE SCHEMA feeds');
GO
IF OBJECT_ID('oms.strategies') IS NULL
CREATE TABLE oms.strategies (
  strategy_id   varchar(8)   NOT NULL PRIMARY KEY,
  name          nvarchar(64) NOT NULL,
  style         varchar(16)  NOT NULL);
GO
IF OBJECT_ID('oms.accounts') IS NULL
CREATE TABLE oms.accounts (
  account_id    varchar(12)  NOT NULL PRIMARY KEY,
  strategy_id   varchar(8)   NOT NULL REFERENCES oms.strategies(strategy_id),
  fund_code     varchar(12)  NOT NULL,
  mandate       nvarchar(64) NULL,
  opened_on     date         NULL);
GO
IF OBJECT_ID('oms.trades') IS NULL
CREATE TABLE oms.trades (
  trade_id      bigint        NOT NULL PRIMARY KEY,
  trade_ts      datetime2(0)  NOT NULL,
  trade_date    date          NOT NULL,
  account_id    varchar(12)   NOT NULL REFERENCES oms.accounts(account_id),
  strategy_id   varchar(8)    NOT NULL,
  company_id    varchar(8)    NOT NULL,
  ticker        varchar(16)   NOT NULL,
  side          char(1)       NOT NULL CHECK (side IN ('B','S')),
  quantity      decimal(18,0) NOT NULL,
  price_local   decimal(18,4) NOT NULL,
  value_usd     decimal(18,2) NOT NULL,
  exec_broker   nvarchar(48)  NULL,
  is_rebalance  bit           NOT NULL DEFAULT 0,
  pm_name       nvarchar(64)  NULL,
  booked_at     datetime2(0)  NOT NULL);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_trades_date') CREATE INDEX ix_trades_date ON oms.trades (trade_date) INCLUDE (company_id, account_id, side, value_usd, is_rebalance);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_trades_company') CREATE INDEX ix_trades_company ON oms.trades (company_id, trade_date);
GO
IF OBJECT_ID('oms.positions') IS NULL
CREATE TABLE oms.positions (
  as_of             date          NOT NULL,
  account_id        varchar(12)   NOT NULL,
  company_id        varchar(8)    NOT NULL,
  quantity          decimal(18,0) NOT NULL,
  market_value_usd  decimal(18,2) NOT NULL,
  weight_bps        int           NOT NULL,
  active_weight_bps int           NULL,
  PRIMARY KEY (as_of, account_id, company_id));
GO
IF OBJECT_ID('research.analyst_views') IS NULL
CREATE TABLE research.analyst_views (
  view_id           int IDENTITY(1,1) PRIMARY KEY,
  company_id        varchar(8)    NOT NULL,
  analyst           nvarchar(64)  NOT NULL,
  rating            varchar(4)    NOT NULL,
  active_weight_bps int           NOT NULL,
  effective_date    date          NOT NULL,
  rationale         nvarchar(1000) NULL);
GO
IF OBJECT_ID('research.notes') IS NULL
CREATE TABLE research.notes (
  note_id       varchar(12)   NOT NULL PRIMARY KEY,
  company_id    varchar(8)    NOT NULL,
  analyst       nvarchar(64)  NOT NULL,
  published_at  datetime2(0)  NOT NULL,
  title         nvarchar(200) NOT NULL,
  body          nvarchar(max) NOT NULL);
GO
IF OBJECT_ID('feeds.news_wire') IS NULL
CREATE TABLE feeds.news_wire (
  news_id       bigint        NOT NULL PRIMARY KEY,
  published_at  datetime2(0)  NOT NULL,
  company_id    varchar(8)    NULL,
  headline      nvarchar(300) NOT NULL,
  source        nvarchar(48)  NOT NULL,
  body          nvarchar(max) NULL,
  tags          nvarchar(200) NULL);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_news_pub') CREATE INDEX ix_news_pub ON feeds.news_wire (published_at) INCLUDE (company_id);
GO
IF OBJECT_ID('feeds.web_index') IS NULL
CREATE TABLE feeds.web_index (
  doc_id        int           NOT NULL PRIMARY KEY,
  url           nvarchar(300) NOT NULL,
  title         nvarchar(300) NOT NULL,
  snippet       nvarchar(1000) NOT NULL,
  published_at  datetime2(0)  NOT NULL,
  company_id    varchar(8)    NULL,
  keywords      nvarchar(300) NULL);
GO
