-- research_notes_search@1
SELECT note_id, company_id, analyst, published_at, title,
       substring(body, GREATEST(locate(lower(:q0), lower(body)) - 120, 1), 420) AS snippet
FROM {fc}.research.notes
WHERE company_id = :company_id AND ({like_clause})
ORDER BY published_at DESC LIMIT {limit}
