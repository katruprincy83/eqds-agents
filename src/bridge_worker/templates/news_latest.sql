-- news_latest@1
SELECT n.news_id, n.published_at, n.company_id, n.headline, n.source, concat('wire://', CAST(n.news_id AS STRING)) AS url
FROM {fc}.feeds.news_wire n
{join} WHERE n.published_at >= :since {filter}
ORDER BY n.published_at DESC LIMIT {limit}
