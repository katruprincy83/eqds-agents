-- Operational state (design §7.3, §5.4, §5.6). Forward-only, idempotent. Applied by job_env_bootstrap.
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS mem;
CREATE SCHEMA IF NOT EXISTS registry;
CREATE SCHEMA IF NOT EXISTS bridge;
CREATE SCHEMA IF NOT EXISTS lg;      -- LangGraph checkpoint tables (created by checkpointer.setup())

-- runs and events (moderator) -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.runs (
  run_id          text PRIMARY KEY,
  conversation_id text NOT NULL,
  user_id         text,
  caller          text,
  question        text NOT NULL,
  rounds          int,
  specialists     text[],
  company_id      text,
  status          text NOT NULL,                 -- QUEUED|RUNNING|DONE|ERROR|CANCELLED
  stage           text,
  heartbeat_at    timestamptz,
  result          jsonb,
  error           text,
  trace_id        text,
  cost_usd        numeric(10,4),
  budget          jsonb,
  created_at      timestamptz DEFAULT now(),
  finished_at     timestamptz);
CREATE INDEX IF NOT EXISTS runs_user_idx   ON ops.runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_idx ON ops.runs (status, heartbeat_at);

CREATE TABLE IF NOT EXISTS ops.run_events (
  run_id     text,
  seq        bigserial,
  type       text,
  payload    jsonb,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (run_id, seq));

CREATE TABLE IF NOT EXISTS ops.wiki_requests (
  id           bigserial PRIMARY KEY,
  company_id   text NOT NULL,
  company_name text,
  requested_by text,
  status       text NOT NULL DEFAULT 'QUEUED',   -- QUEUED|BUILDING|DONE|ERROR
  detail       text,
  created_at   timestamptz DEFAULT now(),
  finished_at  timestamptz);

-- memory ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mem.conversation (
  conversation_id text,
  turn            int,
  question        text,
  answer_summary  text,
  run_id          text,
  created_at      timestamptz DEFAULT now(),
  PRIMARY KEY (conversation_id, turn));

CREATE TABLE IF NOT EXISTS mem.specialist (
  specialist      text,
  conversation_id text,
  caller          text,
  idempotency_key text UNIQUE,
  brief           text,
  response        jsonb,
  created_at      timestamptz DEFAULT now());
CREATE INDEX IF NOT EXISTS specialist_mem_idx ON mem.specialist (specialist, conversation_id, created_at DESC);

-- registry ----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS registry.specialists (
  key              text PRIMARY KEY,
  display_name     text NOT NULL,
  tag_prefix       text NOT NULL UNIQUE,
  description      text NOT NULL,
  domains          text[] NOT NULL,
  url              text NOT NULL,
  contract_version text NOT NULL DEFAULT '1',
  timeout_s        int  NOT NULL DEFAULT 90,
  enabled          boolean NOT NULL DEFAULT true,
  env              text NOT NULL,
  version          text NOT NULL,
  owner            text NOT NULL,
  tools            text,
  app              text,
  updated_at       timestamptz NOT NULL DEFAULT now());

-- bridge queue (design §5.6) ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bridge.requests (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tool        text NOT NULL,
  args        jsonb NOT NULL,
  run_id      text, caller text, user_id text,
  status      text NOT NULL DEFAULT 'PENDING',   -- PENDING|RUNNING|DONE|ERROR|EXPIRED
  result      jsonb, error text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  started_at  timestamptz, finished_at timestamptz,
  expires_at  timestamptz NOT NULL DEFAULT now() + interval '60 seconds');
CREATE INDEX IF NOT EXISTS bridge_req_idx ON bridge.requests (status, created_at);

CREATE OR REPLACE FUNCTION bridge.notify_request() RETURNS trigger AS $$
BEGIN PERFORM pg_notify('bridge', NEW.id::text); RETURN NEW; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS bridge_notify ON bridge.requests;
CREATE TRIGGER bridge_notify AFTER INSERT ON bridge.requests FOR EACH ROW EXECUTE FUNCTION bridge.notify_request();

CREATE OR REPLACE FUNCTION bridge.notify_done() RETURNS trigger AS $$
BEGIN IF NEW.status IN ('DONE','ERROR') THEN PERFORM pg_notify('bridge_done', NEW.id::text); END IF; RETURN NEW; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS bridge_notify_done ON bridge.requests;
CREATE TRIGGER bridge_notify_done AFTER UPDATE ON bridge.requests FOR EACH ROW EXECUTE FUNCTION bridge.notify_done();
