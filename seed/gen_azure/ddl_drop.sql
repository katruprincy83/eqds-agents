-- Removes ONLY the EQDS tables (schemas oms / research / feeds). Used by job_seed_azure for a deterministic full rebuild.
IF OBJECT_ID('oms.trades') IS NOT NULL DROP TABLE oms.trades;
GO
IF OBJECT_ID('oms.positions') IS NOT NULL DROP TABLE oms.positions;
GO
IF OBJECT_ID('oms.accounts') IS NOT NULL DROP TABLE oms.accounts;
GO
IF OBJECT_ID('oms.strategies') IS NOT NULL DROP TABLE oms.strategies;
GO
IF OBJECT_ID('research.analyst_views') IS NOT NULL DROP TABLE research.analyst_views;
GO
IF OBJECT_ID('research.notes') IS NOT NULL DROP TABLE research.notes;
GO
IF OBJECT_ID('feeds.news_wire') IS NOT NULL DROP TABLE feeds.news_wire;
GO
IF OBJECT_ID('feeds.web_index') IS NOT NULL DROP TABLE feeds.web_index;
GO
