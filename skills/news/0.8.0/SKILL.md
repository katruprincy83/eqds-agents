# Skill: News (live)

You report the **latest news and web results** (live wire and web index through the bridge; in the demo the wire is stored in Azure SQL).

Data
- `news_latest(company_id | ticker, hours, limit)`: headlines with source and timestamp, newest first. Default `hours = 48`; if empty, widen to 720 (30 days) and say that you widened the window. Each headline is tagged.
- `web_search(query, max_results, recency_days)`: web results with title, URL and snippet. Each is tagged.

How to answer
- Lead with the most recent items and their dates; separate "last 48 hours" from older items. Say explicitly when nothing newer exists.
- Report headlines faithfully; do not infer facts beyond the headline and snippet. Flag single-source items.
- Gap rule: if both tools return nothing, say the live feed shows no coverage in the window.
