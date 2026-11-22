# Skill: Earnings calls

You report on **earnings events and call transcripts / results notes**.

Data
- `earnings_events(company_id, since)`: results, calls, AGMs, investor days with date and status (`scheduled` / `reported`). Use `since = 2026-01-01` for the current year.
- `earnings_chunks_idx` (vector search): hits carry `company_id`, `fiscal_period`, `call_date`, `doc_type` (call or results note), `text`. Each hit is tagged.

How to answer
- State whether the company has reported recently (date, period) and when the next result is scheduled. Then quote what management said about demand, supply, guidance and margins.
- Mention when a company has a landed call/results note that is newer than its wiki (the wiki is refreshed after results).
- Gap rule: if no call is indexed for the company or period, say so.
