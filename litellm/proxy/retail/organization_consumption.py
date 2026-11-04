from typing import Final

ORGANIZATION_COHORT_SQL: Final = """WITH selected AS MATERIALIZED (
    SELECT request_id,organization_id,assignment_version,organization_version,membership_version,captured_at
    FROM retail_request_organization_snapshots WHERE organization_id=%s AND captured_at>=%s AND captured_at<%s
    ORDER BY captured_at DESC,request_id DESC LIMIT 10001
  ), cohort AS MATERIALIZED (
    SELECT s.request_id,r.user_id,r.model,s.captured_at,s.assignment_version,s.organization_version,s.membership_version,
      r.status,l.amount_nanos AS charge_nanos,
      ((r.status='settled') IS DISTINCT FROM (l.id IS NOT NULL) OR COALESCE(l.amount_nanos>0,false)) AS inconsistent,
      CASE WHEN r.status='settled' THEN 'settled'
        WHEN f.state='conflict' OR f.last_error_type='IdempotencyConflict' THEN 'conflict'
        WHEN f.state='unknown' THEN 'unknown' WHEN r.status='released' THEN 'released' ELSE 'pending' END AS state
    FROM selected s
    JOIN LATERAL (SELECT user_id,model,status FROM retail_reservations WHERE request_id=s.request_id LIMIT 1) r ON true
    LEFT JOIN LATERAL (SELECT state,last_error_type FROM retail_settlement_facts
      WHERE request_id=s.request_id LIMIT 1) f ON true
    LEFT JOIN LATERAL (SELECT id,amount_nanos FROM retail_ledger
      WHERE operation_key='charge:'||s.request_id AND kind='charge' AND request_id=s.request_id
        AND user_id=r.user_id LIMIT 1) l ON true
  )"""
