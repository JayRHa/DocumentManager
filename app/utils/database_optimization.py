"""
Database optimization utilities for indexing and performance.
"""
from sqlalchemy import text, Index, String, cast, func, select
from sqlalchemy.orm import Session
from loguru import logger
from typing import List, Dict, Any

from ..database import engine
from ..models import Document, Correspondent, Tag, DocType, User, AuditLog


TABLES_BY_NAME = {
    'documents': Document.__table__,
    'correspondents': Correspondent.__table__,
    'tags': Tag.__table__,
    'doc_types': DocType.__table__,
    'users': User.__table__,
    'audit_logs': AuditLog.__table__,
}


def _count_table_rows(db: Session, table_name: str) -> int:
    table = TABLES_BY_NAME[table_name]
    return db.execute(select(func.count()).select_from(table)).scalar() or 0


def create_indexes(db: Session) -> List[Dict[str, Any]]:
    """
    Create database indexes for optimal performance.
    
    Returns:
        List of created indexes with status
    """
    indexes_created = []
    
    # Define indexes to create
    indexes = [
        # Document indexes
        Index('idx_document_created_at', Document.created_at),
        Index('idx_document_document_date', Document.document_date),
        Index('idx_document_correspondent_id', Document.correspondent_id),
        Index('idx_document_doc_type_id', Document.doc_type_id),
        Index('idx_document_ocr_status', Document.ocr_status),
        Index('idx_document_ai_status', Document.ai_status),
        Index('idx_document_file_hash', Document.file_hash),
        
        # Composite indexes for common queries
        Index('idx_document_status_created', Document.ocr_status, Document.ai_status, Document.created_at),
        Index('idx_document_correspondent_date', Document.correspondent_id, Document.document_date),
        
        # User indexes
        Index('idx_user_username', User.username, unique=True),
        Index('idx_user_email', User.email),
        Index('idx_user_is_active', User.is_active),
        
        # Audit log indexes
        Index('idx_audit_user_id', AuditLog.user_id),
        Index('idx_audit_action', AuditLog.action),
        Index('idx_audit_created_at', AuditLog.created_at),
        Index('idx_audit_resource', AuditLog.resource_type, AuditLog.resource_id),
        
        # Tag and correspondent indexes
        Index('idx_tag_name', Tag.name, unique=True),
        Index('idx_correspondent_name', Correspondent.name, unique=True),
        Index('idx_doctype_name', DocType.name, unique=True),
    ]
    
    # Create indexes
    for index in indexes:
        try:
            index.create(engine, checkfirst=True)
            indexes_created.append({
                'name': index.name,
                'table': index.table.name if hasattr(index, 'table') else 'unknown',
                'status': 'created',
                'columns': [col.name for col in index.columns]
            })
            logger.info(f"Created index: {index.name}")
        except Exception as e:
            logger.error(f"Failed to create index {index.name}: {e}")
            indexes_created.append({
                'name': index.name,
                'status': 'failed',
                'error': str(e)
            })
    
    # Create full-text search indexes for SQLite
    try:
        # Check if using SQLite
        if 'sqlite' in str(engine.url):
            # Create FTS5 virtual table for document search
            with engine.connect() as conn:
                # Check if FTS table exists
                result = conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='documents_fts'")
                ).fetchone()
                
                if not result:
                    # Create FTS table
                    conn.execute(text("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                            document_id UNINDEXED,
                            title,
                            full_text,
                            summary,
                            content=documents,
                            content_rowid=id
                        )
                    """))
                    
                    # Populate FTS table
                    conn.execute(text("""
                        INSERT INTO documents_fts(document_id, title, full_text, summary)
                        SELECT id, title, full_text, summary FROM documents
                    """))
                    
                    # Create triggers to keep FTS in sync
                    conn.execute(text("""
                        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                            INSERT INTO documents_fts(document_id, title, full_text, summary)
                            VALUES (new.id, new.title, new.full_text, new.summary);
                        END
                    """))
                    
                    conn.execute(text("""
                        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                            DELETE FROM documents_fts WHERE document_id = old.id;
                        END
                    """))
                    
                    conn.execute(text("""
                        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                            UPDATE documents_fts 
                            SET title = new.title, full_text = new.full_text, summary = new.summary
                            WHERE document_id = new.id;
                        END
                    """))
                    
                    conn.commit()
                    
                    indexes_created.append({
                        'name': 'documents_fts',
                        'type': 'FTS5',
                        'status': 'created',
                        'columns': ['title', 'full_text', 'summary']
                    })
                    logger.info("Created FTS5 virtual table for full-text search")
                    
    except Exception as e:
        logger.error(f"Failed to create FTS indexes: {e}")
        indexes_created.append({
            'name': 'documents_fts',
            'status': 'failed',
            'error': str(e)
        })
    
    return indexes_created


def analyze_database(db: Session) -> Dict[str, Any]:
    """
    Analyze database performance and suggest optimizations.
    
    Returns:
        Analysis results and recommendations
    """
    analysis = {
        'table_stats': [],
        'index_usage': [],
        'recommendations': []
    }
    
    try:
        # Get table statistics
        for table, table_obj in TABLES_BY_NAME.items():
            count = db.execute(select(func.count()).select_from(table_obj)).scalar() or 0
            analysis['table_stats'].append({
                'table': table,
                'row_count': count
            })
        
        # Check for missing indexes on foreign keys
        with engine.connect() as conn:
            if 'sqlite' in str(engine.url):
                # SQLite-specific index check
                result = conn.execute(text("""
                    SELECT name, tbl_name, sql 
                    FROM sqlite_master 
                    WHERE type = 'index' AND sql IS NOT NULL
                """))
                
                for row in result:
                    analysis['index_usage'].append({
                        'index_name': row[0],
                        'table_name': row[1],
                        'definition': row[2]
                    })
        
        # Generate recommendations
        doc_count = _count_table_rows(db, 'documents')
        
        if doc_count > 10000:
            analysis['recommendations'].append({
                'priority': 'high',
                'recommendation': 'Consider partitioning documents table by date',
                'reason': f'Document count ({doc_count}) exceeds 10,000'
            })
        
        if doc_count > 5000:
            analysis['recommendations'].append({
                'priority': 'medium',
                'recommendation': 'Enable query result caching',
                'reason': 'Large dataset may benefit from caching'
            })
        
        # Check for slow queries (would need query logging enabled)
        analysis['recommendations'].append({
            'priority': 'low',
            'recommendation': 'Enable query logging to identify slow queries',
            'reason': 'Performance monitoring'
        })
        
    except Exception as e:
        logger.error(f"Database analysis failed: {e}")
        analysis['error'] = str(e)
    
    return analysis


def optimize_database(db: Session) -> Dict[str, Any]:
    """
    Run database optimization tasks.
    
    Returns:
        Optimization results
    """
    results = {
        'vacuum': False,
        'analyze': False,
        'reindex': False,
        'errors': []
    }
    
    try:
        with engine.connect() as conn:
            if 'sqlite' in str(engine.url):
                # SQLite optimizations
                
                # VACUUM to reclaim space
                conn.execute(text("VACUUM"))
                results['vacuum'] = True
                logger.info("Database VACUUM completed")
                
                # ANALYZE to update statistics
                conn.execute(text("ANALYZE"))
                results['analyze'] = True
                logger.info("Database ANALYZE completed")
                
                # REINDEX to rebuild indexes
                conn.execute(text("REINDEX"))
                results['reindex'] = True
                logger.info("Database REINDEX completed")
                
            elif 'postgresql' in str(engine.url):
                # PostgreSQL optimizations
                
                # VACUUM ANALYZE
                conn.execute(text("VACUUM ANALYZE"))
                results['vacuum'] = True
                results['analyze'] = True
                logger.info("PostgreSQL VACUUM ANALYZE completed")
                
                # REINDEX
                preparer = engine.dialect.identifier_preparer
                for table in ('documents', 'users', 'audit_logs'):
                    try:
                        quoted_table = preparer.quote(table)
                        conn.execute(text("REINDEX TABLE " + quoted_table))
                        logger.info(f"Reindexed table: {table}")
                    except Exception as e:
                        results['errors'].append(f"Failed to reindex {table}: {str(e)}")
                
                results['reindex'] = True
                
    except Exception as e:
        logger.error(f"Database optimization failed: {e}")
        results['errors'].append(str(e))
    
    return results


def get_database_size() -> Dict[str, Any]:
    """
    Get database size information.
    
    Returns:
        Database size statistics
    """
    size_info = {}
    
    try:
        if 'sqlite' in str(engine.url):
            # For SQLite, get file size
            import os
            db_path = str(engine.url).replace('sqlite:///', '')
            if os.path.exists(db_path):
                size_bytes = os.path.getsize(db_path)
                size_info['total_size_mb'] = round(size_bytes / (1024 * 1024), 2)
                size_info['total_size_bytes'] = size_bytes
                
                # Get table sizes (approximate)
                with engine.connect() as conn:
                    size_info['tables'] = {}
                    
                    for table in ('documents', 'correspondents', 'tags', 'users', 'audit_logs'):
                        table_obj = TABLES_BY_NAME[table]
                        result = conn.execute(
                            select(
                                func.count().label('count'),
                                func.avg(func.length(cast(table_obj.c.id, String))).label('avg_size'),
                            ).select_from(table_obj)
                        ).fetchone()
                        
                        if result:
                            size_info['tables'][table] = {
                                'row_count': result[0],
                                'estimated_size_kb': round((result[0] * (result[1] or 100)) / 1024, 2)
                            }
                            
        elif 'postgresql' in str(engine.url):
            # PostgreSQL size queries
            with engine.connect() as conn:
                # Total database size
                result = conn.execute(
                    text("SELECT pg_database_size(current_database()) as size")
                ).fetchone()
                
                if result:
                    size_info['total_size_bytes'] = result[0]
                    size_info['total_size_mb'] = round(result[0] / (1024 * 1024), 2)
                
                # Table sizes
                result = conn.execute(text("""
                    SELECT 
                        schemaname,
                        tablename,
                        pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size,
                        pg_total_relation_size(schemaname||'.'||tablename) as size_bytes
                    FROM pg_tables
                    WHERE schemaname = 'public'
                    ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
                """))
                
                size_info['tables'] = {}
                for row in result:
                    size_info['tables'][row[1]] = {
                        'size_pretty': row[2],
                        'size_bytes': row[3],
                        'size_mb': round(row[3] / (1024 * 1024), 2)
                    }
                    
    except Exception as e:
        logger.error(f"Failed to get database size: {e}")
        size_info['error'] = str(e)
    
    return size_info
