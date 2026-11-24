-- web_search@1
SELECT doc_id, title, url, snippet, published_at
FROM {fc}.feeds.web_index
WHERE published_at >= :since AND ({like_clause})
ORDER BY published_at DESC LIMIT {limit}
