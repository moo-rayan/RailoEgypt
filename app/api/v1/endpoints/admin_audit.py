"""
Admin audit log endpoints.

GET  /admin/audit/logs       — List audit log entries (with filters)
GET  /admin/audit/stats      — Summary statistics
GET  /admin/audit/top-ips    — Top offending IPs

All queries use inline literals (no bind parameters) so that asyncpg
sends them through the simple-query protocol.  This avoids the
prepared-statement errors that pgbouncer (transaction mode) causes.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/audit", tags=["Admin Audit"])

# ── Whitelists for safe inline SQL ────────────────────────────────────────────
_VALID_EVENT_TYPES = {
    "rate_limit", "auth_failure", "brute_force", "bot_detected",
    "path_scan", "spam", "attack", "suspicious", "admin_action",
    "forbidden_access", "token_abuse", "invalid_input",
}
_VALID_SEVERITIES = {"info", "warning", "critical"}
_IP_RE = re.compile(r'^[0-9a-fA-F.:]+$')


@router.get("/logs", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_audit_logs(
    db: AsyncSession = Depends(get_db),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    ip_address: Optional[str] = Query(None, description="Filter by IP"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List audit log entries with optional filters."""
    conditions: list[str] = []

    if event_type and event_type in _VALID_EVENT_TYPES:
        conditions.append(f"event_type = '{event_type}'")
    if severity and severity in _VALID_SEVERITIES:
        conditions.append(f"severity = '{severity}'")
    if ip_address and _IP_RE.match(ip_address):
        conditions.append(f"ip_address = '{ip_address}'")

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    safe_limit = int(limit)
    safe_offset = int(offset)

    # Count
    count_q = text(f'SELECT COUNT(*) FROM "EgRailway".audit_log{where_clause}')
    total = (await db.execute(count_q)).scalar() or 0

    # Data
    data_q = text(
        f'SELECT id, created_at, event_type, severity, ip_address, '
        f'user_agent, method, path, status_code, user_id, '
        f'description, metadata, country_code '
        f'FROM "EgRailway".audit_log{where_clause} '
        f'ORDER BY created_at DESC LIMIT {safe_limit} OFFSET {safe_offset}'
    )
    rows = (await db.execute(data_q)).all()

    return {
        "total": total,
        "items": [
            {
                "id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "event_type": r[2],
                "severity": r[3],
                "ip_address": r[4],
                "user_agent": (r[5] or "")[:200],
                "method": r[6],
                "path": r[7],
                "status_code": r[8],
                "user_id": str(r[9]) if r[9] else None,
                "description": r[10],
                "metadata": r[11] or {},
                "country_code": r[12],
            }
            for r in rows
        ],
    }


@router.get("/stats", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_audit_stats(
    db: AsyncSession = Depends(get_db),
    hours: int = Query(24, ge=1, le=720, description="Time window in hours"),
):
    """Summary statistics for the audit log."""
    safe_hours = int(hours)
    q = text(
        f"SELECT "
        f"  COUNT(*) AS total, "
        f"  COUNT(*) FILTER (WHERE severity = 'critical') AS critical, "
        f"  COUNT(*) FILTER (WHERE severity = 'warning') AS warning, "
        f"  COUNT(*) FILTER (WHERE severity = 'info') AS info, "
        f"  COUNT(*) FILTER (WHERE event_type = 'rate_limit') AS rate_limits, "
        f"  COUNT(*) FILTER (WHERE event_type = 'auth_failure') AS auth_failures, "
        f"  COUNT(*) FILTER (WHERE event_type = 'brute_force') AS brute_force, "
        f"  COUNT(*) FILTER (WHERE event_type = 'bot_detected') AS bots, "
        f"  COUNT(*) FILTER (WHERE event_type = 'path_scan') AS path_scans, "
        f"  COUNT(*) FILTER (WHERE event_type = 'spam') AS spam, "
        f"  COUNT(*) FILTER (WHERE event_type = 'attack') AS attacks, "
        f"  COUNT(*) FILTER (WHERE event_type = 'suspicious') AS suspicious, "
        f"  COUNT(*) FILTER (WHERE event_type = 'admin_action') AS admin_actions, "
        f"  COUNT(*) FILTER (WHERE event_type = 'forbidden_access') AS forbidden, "
        f"  COUNT(DISTINCT ip_address) AS unique_ips "
        f'FROM "EgRailway".audit_log '
        f"WHERE created_at >= NOW() - make_interval(hours => {safe_hours})"
    )
    row = (await db.execute(q)).one()

    return {
        "window_hours": hours,
        "total": row[0],
        "by_severity": {
            "critical": row[1],
            "warning": row[2],
            "info": row[3],
        },
        "by_type": {
            "rate_limit": row[4],
            "auth_failure": row[5],
            "brute_force": row[6],
            "bot_detected": row[7],
            "path_scan": row[8],
            "spam": row[9],
            "attack": row[10],
            "suspicious": row[11],
            "admin_action": row[12],
            "forbidden_access": row[13],
        },
        "unique_ips": row[14],
    }


@router.get("/top-ips", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_top_offending_ips(
    db: AsyncSession = Depends(get_db),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    """Top offending IPs by event count."""
    safe_hours = int(hours)
    safe_limit = int(limit)
    q = text(
        f"SELECT "
        f"  ip_address, "
        f"  COUNT(*) AS event_count, "
        f"  COUNT(DISTINCT event_type) AS event_types, "
        f"  MAX(severity) AS max_severity, "
        f"  MIN(created_at) AS first_seen, "
        f"  MAX(created_at) AS last_seen, "
        f"  country_code "
        f'FROM "EgRailway".audit_log '
        f"WHERE created_at >= NOW() - make_interval(hours => {safe_hours}) "
        f"  AND ip_address IS NOT NULL "
        f"GROUP BY ip_address, country_code "
        f"ORDER BY event_count DESC "
        f"LIMIT {safe_limit}"
    )
    rows = (await db.execute(q)).all()

    return {
        "window_hours": hours,
        "items": [
            {
                "ip_address": r[0],
                "event_count": r[1],
                "event_types": r[2],
                "max_severity": r[3],
                "first_seen": r[4].isoformat() if r[4] else None,
                "last_seen": r[5].isoformat() if r[5] else None,
                "country_code": r[6],
            }
            for r in rows
        ],
    }
