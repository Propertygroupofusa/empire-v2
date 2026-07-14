"""
Data Retention & Archival System
Ensures all monitor data is kept FOREVER with archival strategy
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy import text
import json

log = logging.getLogger("data_retention")

class DataRetentionManager:
    """Manages permanent data storage and archival"""
    
    def __init__(self):
        self.total_errors_ever = 0
        self.total_fixes_ever = 0
        self.total_metrics_ever = 0
        self.last_archive = None

    async def initialize_retention_tables(self, engine):
        """Create archive tables for permanent storage"""
        async with engine.begin() as conn:
            # Archive tables - keep forever
            try:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS monitor_errors_archive (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        error_type VARCHAR NOT NULL,
                        error_message TEXT NOT NULL,
                        severity VARCHAR,
                        detected_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                log.info("✓ Archive table: monitor_errors_archive (PERMANENT)")
            except Exception as e:
                log.warning(f"Archive table skip: {e}")

            try:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS monitor_fixed_issues_archive (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        issue_name VARCHAR NOT NULL,
                        fixed_at TIMESTAMP,
                        status VARCHAR,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                log.info("✓ Archive table: monitor_fixed_issues_archive (PERMANENT)")
            except Exception as e:
                log.warning(f"Archive table skip: {e}")

            try:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS monitor_performance_archive (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        metric_data TEXT NOT NULL,
                        checked_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                log.info("✓ Archive table: monitor_performance_archive (PERMANENT)")
            except Exception as e:
                log.warning(f"Archive table skip: {e}")

            # Data retention log table
            try:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS data_retention_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action VARCHAR NOT NULL,
                        table_name VARCHAR,
                        records_archived INTEGER,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                log.info("✓ Data retention log table created (PERMANENT)")
            except Exception as e:
                log.warning(f"Retention log skip: {e}")

    async def archive_old_data(self, engine, days_threshold=90):
        """
        Archive old data (older than threshold) to archive tables
        BUT NEVER DELETE - just move to archive for performance
        """
        try:
            from database import engine as db_engine
            async with db_engine.begin() as conn:
                threshold_date = (datetime.now() - timedelta(days=days_threshold)).isoformat()
                
                # Archive old errors
                result = await conn.execute(text("""
                    SELECT COUNT(*) FROM monitor_errors
                    WHERE created_at < :threshold
                """), {"threshold": threshold_date})
                old_errors_count = result.scalar()
                
                if old_errors_count > 0:
                    await conn.execute(text("""
                        INSERT INTO monitor_errors_archive
                        SELECT * FROM monitor_errors
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        DELETE FROM monitor_errors
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        INSERT INTO data_retention_log (action, table_name, records_archived)
                        VALUES ('archive', 'monitor_errors', :count)
                    """), {"count": old_errors_count})
                    
                    log.info(f"📦 Archived {old_errors_count} old errors to permanent archive")
                
                # Archive old fixes
                result = await conn.execute(text("""
                    SELECT COUNT(*) FROM monitor_fixed_issues
                    WHERE created_at < :threshold
                """), {"threshold": threshold_date})
                old_fixes_count = result.scalar()
                
                if old_fixes_count > 0:
                    await conn.execute(text("""
                        INSERT INTO monitor_fixed_issues_archive
                        SELECT * FROM monitor_fixed_issues
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        DELETE FROM monitor_fixed_issues
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        INSERT INTO data_retention_log (action, table_name, records_archived)
                        VALUES ('archive', 'monitor_fixed_issues', :count)
                    """), {"count": old_fixes_count})
                    
                    log.info(f"📦 Archived {old_fixes_count} old fixes to permanent archive")
                
                # Archive old metrics (keep less of these due to size)
                result = await conn.execute(text("""
                    SELECT COUNT(*) FROM monitor_performance
                    WHERE created_at < :threshold
                """), {"threshold": threshold_date})
                old_metrics_count = result.scalar()
                
                if old_metrics_count > 0:
                    await conn.execute(text("""
                        INSERT INTO monitor_performance_archive
                        SELECT * FROM monitor_performance
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        DELETE FROM monitor_performance
                        WHERE created_at < :threshold
                    """), {"threshold": threshold_date})
                    
                    await conn.execute(text("""
                        INSERT INTO data_retention_log (action, table_name, records_archived)
                        VALUES ('archive', 'monitor_performance', :count)
                    """), {"count": old_metrics_count})
                    
                    log.info(f"📦 Archived {old_metrics_count} old metrics to permanent archive")
                
                self.last_archive = datetime.now()
                
                # Log to retention log
                await conn.execute(text("""
                    INSERT INTO data_retention_log (action, table_name, records_archived)
                    VALUES ('archive_complete', 'all_tables', :total)
                """), {"total": old_errors_count + old_fixes_count + old_metrics_count})
                
        except Exception as e:
            log.error(f"Archive error: {e}")

    async def get_total_data_stored(self, engine):
        """Get complete count of all data ever stored"""
        try:
            from database import engine as db_engine
            async with db_engine.begin() as conn:
                # Current tables
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_errors"))
                current_errors = result.scalar() or 0
                
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_fixed_issues"))
                current_fixes = result.scalar() or 0
                
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_performance"))
                current_metrics = result.scalar() or 0
                
                # Archive tables
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_errors_archive"))
                archived_errors = result.scalar() or 0
                
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_fixed_issues_archive"))
                archived_fixes = result.scalar() or 0
                
                result = await conn.execute(text("SELECT COUNT(*) FROM monitor_performance_archive"))
                archived_metrics = result.scalar() or 0
                
                return {
                    "current": {
                        "errors": current_errors,
                        "fixes": current_fixes,
                        "metrics": current_metrics,
                        "total": current_errors + current_fixes + current_metrics
                    },
                    "archived": {
                        "errors": archived_errors,
                        "fixes": archived_fixes,
                        "metrics": archived_metrics,
                        "total": archived_errors + archived_fixes + archived_metrics
                    },
                    "total_forever": (current_errors + archived_errors + current_fixes + 
                                     archived_fixes + current_metrics + archived_metrics)
                }
        except Exception as e:
            log.error(f"Error getting data counts: {e}")
            return {"error": str(e)}

    async def get_retention_status(self, engine):
        """Get data retention and archival status"""
        try:
            from database import engine as db_engine
            async with db_engine.begin() as conn:
                result = await conn.execute(text("""
                    SELECT action, table_name, records_archived, timestamp
                    FROM data_retention_log
                    ORDER BY timestamp DESC
                    LIMIT 20
                """))
                logs = [dict(row._mapping) for row in result.fetchall()]
                
                return {
                    "retention_policy": "KEEP FOREVER - Nothing is ever deleted",
                    "archival_strategy": "Moved to archive tables after 90 days for performance",
                    "last_archive": self.last_archive.isoformat() if self.last_archive else None,
                    "recent_archive_logs": logs
                }
        except Exception as e:
            return {"error": str(e)}

    async def get_database_size(self, engine):
        """Get database size and growth stats"""
        try:
            import os
            db_path = "empire.db"
            if os.path.exists(db_path):
                size_bytes = os.path.getsize(db_path)
                size_mb = size_bytes / (1024 * 1024)
                return {
                    "database_file": db_path,
                    "size_bytes": size_bytes,
                    "size_mb": round(size_mb, 2),
                    "size_gb": round(size_mb / 1024, 2)
                }
        except Exception as e:
            return {"error": str(e)}


retention_manager = DataRetentionManager()
