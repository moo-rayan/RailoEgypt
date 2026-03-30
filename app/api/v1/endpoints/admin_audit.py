"""
Admin audit log endpoints.

GET  /admin/audit/logs       — List audit log entries (with filters)
GET  /admin/audit/stats      — Summary statistics
GET  /admin/audit/top-ips    — Top offending IPs
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/audit", tags=["Admin Audit"])


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
    conditions = []
    params: dict = {"lim": limit, "off": offset}

    if event_type:
        conditions.append("event_type = :et")
        params["et"] = event_type
    if severity:
        conditions.append("severity = :sev")
        params["sev"] = severity
    if ip_address:
        conditions.append("ip_address = :ip")
        params["ip"] = ip_address

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # Count
    count_q = text(f'SELECT COUNT(*) FROM "EgRailway".audit_log{where_clause}')
    total = (await db.execute(count_q, params)).scalar() or 0

    # Data
    data_q = text(
        f'SELECT id, created_at, event_type, severity, ip_address, '
        f'user_agent, method, path, status_code, user_id, '
        f'description, metadata, country_code '
        f'FROM "EgRailway".audit_log{where_clause} '
        f'ORDER BY created_at DESC LIMIT :lim OFFSET :off'
    )
    rows = (await db.execute(data_q, params)).all()

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
    q = text(
        'SELECT '
        '  COUNT(*) AS total, '
        '  COUNT(*) FILTER (WHERE severity = \'critical\') AS critical, '
        '  COUNT(*) FILTER (WHERE severity = \'warning\') AS warning, '
        '  COUNT(*) FILTER (WHERE severity = \'info\') AS info, '
        '  COUNT(*) FILTER (WHERE event_type = \'rate_limit\') AS rate_limits, '
        '  COUNT(*) FILTER (WHERE event_type = \'auth_failure\') AS auth_failures, '
        '  COUNT(*) FILTER (WHERE event_type = \'brute_force\') AS brute_force, '
        '  COUNT(*) FILTER (WHERE event_type = \'bot_detected\') AS bots, '
        '  COUNT(*) FILTER (WHERE event_type = \'path_scan\') AS path_scans, '
        '  COUNT(*) FILTER (WHERE event_type = \'spam\') AS spam, '
        '  COUNT(*) FILTER (WHERE event_type = \'attack\') AS attacks, '
        '  COUNT(*) FILTER (WHERE event_type = \'suspicious\') AS suspicious, '
        '  COUNT(*) FILTER (WHERE event_type = \'admin_action\') AS admin_actions, '
        '  COUNT(*) FILTER (WHERE event_type = \'forbidden_access\') AS forbidden, '
        '  COUNT(DISTINCT ip_address) AS unique_ips '
        'FROM "EgRailway".audit_log '
        'WHERE created_at >= NOW() - make_interval(hours => :hrs)'
    )
    row = (await db.execute(q, {"hrs": hours})).one()

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
    q = text(
        'SELECT '
        '  ip_address, '
        '  COUNT(*) AS event_count, '
        '  COUNT(DISTINCT event_type) AS event_types, '
        '  MAX(severity) AS max_severity, '
        '  MIN(created_at) AS first_seen, '
        '  MAX(created_at) AS last_seen, '
        '  country_code '
        'FROM "EgRailway".audit_log '
        'WHERE created_at >= NOW() - make_interval(hours => :hrs) '
        '  AND ip_address IS NOT NULL '
        'GROUP BY ip_address, country_code '
        'ORDER BY event_count DESC '
        'LIMIT :lim'
    )
    rows = (await db.execute(q, {"hrs": hours, "lim": limit})).all()

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
