-- Specialist memory and idempotency cache (AgentMemory). Applied inside the agent's own schema agent_<key>
-- (search_path is set by AgentMemory.migrate), so the names are unqualified.
CREATE TABLE IF NOT EXISTS memory (
  idempotency_key text PRIMARY KEY,           -- caller's key (Idempotency-Key header or custom_inputs.idempotency_key)
  conversation_id text,
  caller          text,
  brief           text,
  response        jsonb,
  created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_conversation_idx ON memory (conversation_id, created_at DESC);
