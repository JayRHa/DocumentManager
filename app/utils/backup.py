"""
Database and file backup utilities.
"""
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
import json
import tarfile
import tempfile
from loguru import logger

from ..database import engine
from ..config import get_settings
from ..models import User


class BackupError(Exception):
    """Backup operation error"""
    pass


def _safe_extractall(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tar archive into ``dest`` while blocking path traversal.

    Older Python releases (< 3.12) don't support the ``filter='data'``
    argument to :py:meth:`tarfile.TarFile.extractall`, so we manually
    validate each member's destination against the target directory.
    Members whose resolved path escapes ``dest``, absolute paths, and
    symlinks/hard-links that point outside ``dest`` are rejected.

    See CVE-2007-4559 / PEP 706 for background.
    """
    dest_root = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest_root / member.name).resolve()
        try:
            member_path.relative_to(dest_root)
        except ValueError:
            raise BackupError(
                f"Refusing to extract tar member outside dest: {member.name!r}"
            )
        if member.issym() or member.islnk():
            link_target = (member_path.parent / member.linkname).resolve()
            try:
                link_target.relative_to(dest_root)
            except ValueError:
                raise BackupError(
                    f"Refusing to extract tar link outside dest: {member.name!r}"
                    f" -> {member.linkname!r}"
                )
    tar.extractall(dest)


def create_backup(
    db_session,
    backup_name: Optional[str] = None,
    include_files: bool = True,
    user: Optional[User] = None
) -> Dict[str, Any]:
    """
    Create a complete system backup including database and files.
    
    Args:
        db_session: Database session
        backup_name: Optional custom backup name
        include_files: Whether to include document files
        user: User initiating the backup
        
    Returns:
        Backup information dictionary
    """
    settings = get_settings(db_session)
    
    # Generate backup name with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = backup_name or f"backup_{timestamp}"
    
    # Create backup directory
    backup_base = Path(settings.get('backup_path', 'data/backups'))
    backup_base.mkdir(exist_ok=True)
    backup_dir = backup_base / backup_name
    backup_dir.mkdir(exist_ok=True)
    
    backup_info = {
        'name': backup_name,
        'timestamp': timestamp,
        'path': str(backup_dir),
        'database_backup': None,
        'files_backup': None,
        'metadata': None,
        'errors': []
    }
    
    try:
        # 1. Backup database
        logger.info(f"Starting database backup to {backup_dir}")
        db_backup_path = backup_database(backup_dir)
        backup_info['database_backup'] = str(db_backup_path)
        
        # 2. Backup files if requested
        if include_files:
            logger.info("Backing up document files")
            files_backup_path = backup_files(backup_dir, settings)
            backup_info['files_backup'] = str(files_backup_path)
        
        # 3. Create metadata file
        metadata = {
            'backup_name': backup_name,
            'created_at': datetime.now().isoformat(),
            'created_by': user.username if user else 'system',
            'database_engine': str(engine.url).split('://')[0],
            'include_files': include_files,
            'settings': {
                'storage_folder': settings.get('storage_folder'),
                'staging_folder': settings.get('staging_folder'),
            },
            'statistics': get_backup_statistics(db_session)
        }
        
        metadata_path = backup_dir / 'metadata.json'
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        backup_info['metadata'] = metadata
        
        # 4. Create compressed archive
        archive_path = create_backup_archive(backup_dir, backup_base)
        backup_info['archive_path'] = str(archive_path)
        backup_info['archive_size_mb'] = round(archive_path.stat().st_size / (1024 * 1024), 2)
        
        # 5. Clean up uncompressed backup directory
        shutil.rmtree(backup_dir)
        
        logger.info(f"Backup completed successfully: {archive_path}")
        
        # Log backup event
        if user:
            from ..services.audit_service import log_audit_event
            log_audit_event(
                db=db_session,
                user_id=user.id,
                action="backup.create",
                resource_type="system",
                resource_id=None,
                details={
                    'backup_name': backup_name,
                    'archive_path': str(archive_path),
                    'size_mb': backup_info['archive_size_mb'],
                    'include_files': include_files
                }
            )
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        backup_info['errors'].append(str(e))
        
        # Clean up on failure
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        
        raise BackupError(f"Backup failed: {str(e)}")
    
    return backup_info


def backup_database(backup_dir: Path) -> Path:
    """
    Backup the database to the specified directory.
    
    Args:
        backup_dir: Directory to save the backup
        
    Returns:
        Path to the database backup file
    """
    db_url = str(engine.url)
    
    if 'sqlite' in db_url:
        # SQLite backup - simple file copy
        db_path = db_url.replace('sqlite:///', '')
        if not os.path.exists(db_path):
            raise BackupError(f"Database file not found: {db_path}")
        
        backup_path = backup_dir / 'database.db'
        
        # Use SQLite backup API for consistency
        import sqlite3
        
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(str(backup_path))
        
        with dest:
            source.backup(dest)
        
        source.close()
        dest.close()
        
        return backup_path
        
    elif 'postgresql' in db_url:
        # PostgreSQL backup using pg_dump
        backup_path = backup_dir / 'database.sql'
        
        # Parse connection details
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        
        env = os.environ.copy()
        env['PGPASSWORD'] = parsed.password or ''
        
        cmd = [
            'pg_dump',
            '-h', parsed.hostname or 'localhost',
            '-p', str(parsed.port or 5432),
            '-U', parsed.username or 'postgres',
            '-d', parsed.path.lstrip('/'),
            '-f', str(backup_path),
            '--verbose',
            '--no-owner',
            '--no-privileges'
        ]
        
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {result.stderr}")
        
        return backup_path
        
    else:
        raise BackupError(f"Unsupported database type: {db_url.split('://')[0]}")


def backup_files(backup_dir: Path, settings: Dict[str, Any]) -> Path:
    """
    Backup document files to the specified directory.
    
    Args:
        backup_dir: Directory to save the backup
        settings: Application settings
        
    Returns:
        Path to the files backup
    """
    files_backup_dir = backup_dir / 'files'
    files_backup_dir.mkdir(exist_ok=True)
    
    # Backup storage folder
    storage_path = Path(settings.get('storage_folder', 'data/storage'))
    if storage_path.exists():
        storage_backup = files_backup_dir / 'storage'
        shutil.copytree(storage_path, storage_backup, dirs_exist_ok=True)
        logger.info(f"Backed up storage folder: {storage_path}")
    
    # Backup staging folder (optional)
    staging_path = Path(settings.get('staging_folder', 'data/staging'))
    if staging_path.exists():
        staging_backup = files_backup_dir / 'staging'
        shutil.copytree(staging_path, staging_backup, dirs_exist_ok=True)
        logger.info(f"Backed up staging folder: {staging_path}")
    
    return files_backup_dir


def create_backup_archive(backup_dir: Path, output_dir: Path) -> Path:
    """
    Create a compressed archive of the backup.
    
    Args:
        backup_dir: Directory containing the backup
        output_dir: Directory to save the archive
        
    Returns:
        Path to the created archive
    """
    archive_name = f"{backup_dir.name}.tar.gz"
    archive_path = output_dir / archive_name
    
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(backup_dir, arcname=backup_dir.name)
    
    return archive_path


def restore_backup(
    archive_path: Path,
    db_session,
    restore_files: bool = True,
    user: Optional[User] = None
) -> Dict[str, Any]:
    """
    Restore a system backup from archive.
    
    Args:
        archive_path: Path to the backup archive
        db_session: Database session
        restore_files: Whether to restore document files
        user: User initiating the restore
        
    Returns:
        Restore information dictionary
    """
    if not archive_path.exists():
        raise BackupError(f"Backup archive not found: {archive_path}")
    
    restore_info = {
        'archive_path': str(archive_path),
        'restored_at': datetime.now().isoformat(),
        'database_restored': False,
        'files_restored': False,
        'errors': []
    }
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        try:
            # Extract archive. Use the 'data' extraction filter introduced
            # in Python 3.12 (PEP 706) to block path traversal, absolute
            # paths, symlinks pointing outside the destination, and special
            # file types inside the tarball (CVE-2007-4559 family of bugs).
            # We pass it as a positional/keyword argument; on Python < 3.12
            # we fall back to a manual safety check.
            logger.info(f"Extracting backup archive: {archive_path}")
            with tarfile.open(archive_path, "r:gz") as tar:
                try:
                    tar.extractall(temp_path, filter="data")
                except TypeError:
                    # Older Python without filter= support: validate members
                    # manually before extracting to prevent path traversal.
                    _safe_extractall(tar, temp_path)
            
            # Find extracted backup directory
            backup_dirs = list(temp_path.iterdir())
            if not backup_dirs:
                raise BackupError("No backup directory found in archive")
            
            backup_dir = backup_dirs[0]
            
            # Read metadata
            metadata_path = backup_dir / 'metadata.json'
            if metadata_path.exists():
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                    restore_info['metadata'] = metadata
            
            # Restore database
            logger.info("Restoring database")
            restore_database(backup_dir)
            restore_info['database_restored'] = True
            
            # Restore files if requested
            if restore_files and (backup_dir / 'files').exists():
                logger.info("Restoring document files")
                restore_files_from_backup(backup_dir, db_session)
                restore_info['files_restored'] = True
            
            # Log restore event
            if user:
                from ..services.audit_service import log_audit_event
                log_audit_event(
                    db=db_session,
                    user_id=user.id,
                    action="backup.restore",
                    resource_type="system",
                    resource_id=None,
                    details={
                        'archive_path': str(archive_path),
                        'restore_info': restore_info
                    }
                )
            
            logger.info("Backup restore completed successfully")
            
        except Exception as e:
            logger.error(f"Restore failed: {e}")
            restore_info['errors'].append(str(e))
            raise BackupError(f"Restore failed: {str(e)}")
    
    return restore_info


def restore_database(backup_dir: Path):
    """
    Restore database from backup.
    
    Args:
        backup_dir: Directory containing the backup
    """
    db_url = str(engine.url)
    
    if 'sqlite' in db_url:
        # SQLite restore - replace file
        backup_path = backup_dir / 'database.db'
        if not backup_path.exists():
            raise BackupError("Database backup file not found")
        
        db_path = db_url.replace('sqlite:///', '')
        
        # Create backup of current database
        if os.path.exists(db_path):
            shutil.copy2(db_path, f"{db_path}.before_restore")
        
        # Restore database
        shutil.copy2(backup_path, db_path)
        
    elif 'postgresql' in db_url:
        # PostgreSQL restore using psql
        backup_path = backup_dir / 'database.sql'
        if not backup_path.exists():
            raise BackupError("Database backup file not found")
        
        # Parse connection details
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        
        env = os.environ.copy()
        env['PGPASSWORD'] = parsed.password or ''
        
        cmd = [
            'psql',
            '-h', parsed.hostname or 'localhost',
            '-p', str(parsed.port or 5432),
            '-U', parsed.username or 'postgres',
            '-d', parsed.path.lstrip('/'),
            '-f', str(backup_path)
        ]
        
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise BackupError(f"psql restore failed: {result.stderr}")
    
    else:
        raise BackupError(f"Unsupported database type: {db_url.split('://')[0]}")


def restore_files_from_backup(backup_dir: Path, db_session):
    """
    Restore document files from backup.
    
    Args:
        backup_dir: Directory containing the backup
        db_session: Database session
    """
    settings = get_settings(db_session)
    files_backup_dir = backup_dir / 'files'
    
    # Restore storage folder
    storage_backup = files_backup_dir / 'storage'
    if storage_backup.exists():
        storage_path = Path(settings.get('storage_folder', 'data/storage'))
        
        # Backup current files
        if storage_path.exists():
            backup_current = storage_path.parent / f"{storage_path.name}_before_restore"
            if backup_current.exists():
                shutil.rmtree(backup_current)
            shutil.move(storage_path, backup_current)
        
        # Restore files
        shutil.copytree(storage_backup, storage_path)
        logger.info(f"Restored storage folder to: {storage_path}")
    
    # Restore staging folder
    staging_backup = files_backup_dir / 'staging'
    if staging_backup.exists():
        staging_path = Path(settings.get('staging_folder', 'data/staging'))
        
        # Clear current staging
        if staging_path.exists():
            shutil.rmtree(staging_path)
        
        # Restore files
        shutil.copytree(staging_backup, staging_path)
        logger.info(f"Restored staging folder to: {staging_path}")


def get_backup_statistics(db_session) -> Dict[str, int]:
    """
    Get statistics for backup metadata.
    
    Args:
        db_session: Database session
        
    Returns:
        Dictionary of statistics
    """
    from sqlalchemy import func
    from ..models import Document, Correspondent, Tag, User
    
    stats = {
        'total_documents': db_session.query(func.count(Document.id)).scalar() or 0,
        'total_correspondents': db_session.query(func.count(Correspondent.id)).scalar() or 0,
        'total_tags': db_session.query(func.count(Tag.id)).scalar() or 0,
        'total_users': db_session.query(func.count(User.id)).scalar() or 0,
    }
    
    return stats


def list_backups(backup_base: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    List available backups.
    
    Args:
        backup_base: Base backup directory (uses default if not provided)
        
    Returns:
        List of backup information
    """
    if not backup_base:
        backup_base = Path('data/backups')
    
    backups: List[Dict[str, Any]] = []
    try:
        base = Path(backup_base)
        if not base.exists():
            return backups
        
        # Find tar.gz archives
        files = sorted(base.glob('*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            info: Dict[str, Any] = {
                'filename': f.name,
                'size_mb': round(f.stat().st_size / (1024 * 1024), 2),
            }
            # Try to read metadata.json from archive
            try:
                with tarfile.open(f, 'r:gz') as tar:
                    # metadata.json may be at root of backup folder inside tar, find it
                    member = None
                    for m in tar.getmembers():
                        if m.name.endswith('/metadata.json') or m.name == 'metadata.json':
                            member = m
                            break
                    if member:
                        extracted = tar.extractfile(member)
                        if extracted:
                            meta = json.loads(extracted.read().decode('utf-8'))
                            info.update({
                                'created_at': meta.get('created_at'),
                                'created_by': meta.get('created_by'),
                                'statistics': meta.get('statistics', {}),
                                'include_files': meta.get('include_files', True),
                            })
            except Exception as e:
                # Don't fail listing on metadata errors
                logger.debug(f"Could not read metadata for {f.name}: {e}")
            
            backups.append(info)
        
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
    
    return backups

    backups = []
    
    if backup_base.exists():
        for archive_path in backup_base.glob('backup_*.tar.gz'):
            try:
                # Extract metadata without full extraction
                with tarfile.open(archive_path, "r:gz") as tar:
                    # Look for metadata.json
                    for member in tar.getmembers():
                        if member.name.endswith('metadata.json'):
                            f = tar.extractfile(member)
                            if f:
                                metadata = json.load(f)
                                backups.append({
                                    'filename': archive_path.name,
                                    'path': str(archive_path),
                                    'size_mb': round(archive_path.stat().st_size / (1024 * 1024), 2),
                                    'created_at': metadata.get('created_at'),
                                    'created_by': metadata.get('created_by'),
                                    'statistics': metadata.get('statistics', {})
                                })
                                break
            except Exception as e:
                logger.error(f"Error reading backup {archive_path}: {e}")
                backups.append({
                    'filename': archive_path.name,
                    'path': str(archive_path),
                    'size_mb': round(archive_path.stat().st_size / (1024 * 1024), 2),
                    'error': str(e)
                })
    
    # Sort by creation date (newest first)
    backups.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    
    return backups
