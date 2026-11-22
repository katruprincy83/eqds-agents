-- Earnings calendar for a company: scheduled and reported events (drives the earnings specialist and wiki triggers).
CREATE OR REPLACE FUNCTION {uc}.earnings_events(company_id STRING COMMENT 'company id such as C0101', since DATE COMMENT 'only events on or after this date')
RETURNS TABLE (company_id STRING, name STRING, fiscal_period STRING, event_type STRING, event_date DATE, status STRING, source_ref STRING)
COMMENT 'Earnings calendar events (results, calls) with status scheduled/reported. SYNTHETIC DEMO DATA.'
RETURN
  SELECT e.company_id, c.name, e.fiscal_period, e.event_type, e.event_date, e.status, '{uc}.earnings_calendar'
  FROM {uc}.earnings_calendar e JOIN {uc}.ref_companies c USING (company_id)
  WHERE e.company_id = earnings_events.company_id AND e.event_date >= earnings_events.since
  ORDER BY e.event_date DESC
