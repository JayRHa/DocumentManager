"""
Backup management API endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional
from pathlib import Path

import re

from ..database import get_db
from ..models import User
from ..services.auth_service import require_admin_flexible
from ..services.backup_scheduler import backup_scheduler
from ..utils.backup import restore_backup, list_backups
from pydantic import BaseModel


_BACKUP_DIR = Path("data/backups")
_BACKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _resolve_backup_path(backup_filename: str) -> Path:
    """Resolve a user-supplied backup filename to a safe path.

    Prevents path traversal by:
    - rejecting filenames containing path separators / null bytes / dots
      that would let the user escape the directory; and
    - confirming the fully-resolved path stays inside the backups dir.
    """
    if not backup_filename or not _BACKUP_FILENAME_RE.fullmatch(backup_filename):
        raise HTTPException(status_code=400, detail="Invalid backup filename")

    base = _BACKUP_DIR.resolve()
    candidate = (base / backup_filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup filename")
    return candidate

router = APIRouter()


class BackupConfigRequest(BaseModel):
    """Request model for backup configuration"""
    enabled: bool = True
    interval_hours: int = 24
    max_backups: int = 7
    include_files: bool = True
    backup_path: str = "backups"


class ManualBackupRequest(BaseModel):
    """Request model for manual backup"""
    name: Optional[str] = None
    include_files: Optional[bool] = None


@router.get("/status")
def get_backup_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Get backup system status and configuration"""
    return backup_scheduler.get_status()


@router.post("/configure")
def configure_backup_scheduler(
    config: BackupConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Configure automated backup settings"""
    try:
        backup_scheduler.configure(
            enabled=config.enabled,
            interval_hours=config.interval_hours,
            max_backups=config.max_backups,
            include_files=config.include_files,
            backup_path=config.backup_path
        )
        
        # Log configuration change
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="backup.configure",
            resource_type="system",
            resource_id=None,
            details=config.dict()
        )
        
        return {
            "message": "Backup configuration updated successfully",
            "configuration": config.dict()
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to configure backup: {str(e)}")


@router.post("/start")
def start_backup_scheduler(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Start the automated backup scheduler"""
    try:
        backup_scheduler.start()
        
        # Log action
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="backup.scheduler_start",
            resource_type="system",
            resource_id=None,
            details={}
        )
        
        return {"message": "Backup scheduler started successfully"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start backup scheduler: {str(e)}")


@router.post("/stop")
def stop_backup_scheduler(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Stop the automated backup scheduler"""
    try:
        backup_scheduler.stop()
        
        # Log action
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="backup.scheduler_stop",
            resource_type="system",
            resource_id=None,
            details={}
        )
        
        return {"message": "Backup scheduler stopped successfully"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop backup scheduler: {str(e)}")


@router.post("/create")
def create_manual_backup(
    backup_request: ManualBackupRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Create a manual backup"""
    try:
        # Run backup in background
        def run_backup():
            try:
                backup_info = backup_scheduler.force_backup(
                    include_files=backup_request.include_files
                )
                
                # Log backup creation
                from ..services.audit_service import log_audit_event
                with next(get_db()) as audit_db:
                    log_audit_event(
                        db=audit_db,
                        user_id=current_user.id,
                        action="backup.create_manual",
                        resource_type="system",
                        resource_id=None,
                        details={
                            "backup_name": backup_info.get("name"),
                            "include_files": backup_request.include_files,
                            "success": not backup_info.get("errors")
                        }
                    )
            except Exception as e:
                # Log error
                from ..services.audit_service import log_audit_event
                with next(get_db()) as audit_db:
                    log_audit_event(
                        db=audit_db,
                        user_id=current_user.id,
                        action="backup.create_manual_failed",
                        resource_type="system",
                        resource_id=None,
                        details={"error": str(e)}
                    )
        
        background_tasks.add_task(run_backup)
        
        return {
            "message": "Manual backup started in background",
            "status": "initiated"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initiate backup: {str(e)}")


@router.get("/list")
def list_available_backups(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """List all available backup files"""
    try:
        backups = list_backups()
        
        return {
            "backups": backups,
            "total_count": len(backups),
            "total_size_mb": sum(backup.get("size_mb", 0) for backup in backups)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list backups: {str(e)}")


@router.post("/restore/{backup_filename}")
def restore_backup_from_file(
    backup_filename: str,
    restore_files: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Restore system from a backup file"""
    try:
        # Validate backup file exists (with path-traversal protection)
        backup_path = _resolve_backup_path(backup_filename)
        if not backup_path.exists():
            raise HTTPException(status_code=404, detail="Backup file not found")

        # Perform restore
        restore_info = restore_backup(
            archive_path=backup_path,
            db_session=db,
            restore_files=restore_files,
            user=current_user
        )
        
        # Log restore action
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="backup.restore",
            resource_type="system",
            resource_id=None,
            details={
                "backup_filename": backup_filename,
                "restore_files": restore_files,
                "success": not restore_info.get("errors")
            }
        )
        
        return {
            "message": "Backup restored successfully",
            "restore_info": restore_info
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restore backup: {str(e)}")


@router.delete("/delete/{backup_filename}")
def delete_backup_file(
    backup_filename: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Delete a backup file"""
    try:
        backup_path = _resolve_backup_path(backup_filename)
        if not backup_path.exists():
            raise HTTPException(status_code=404, detail="Backup file not found")

        # Get file size before deletion
        file_size_mb = backup_path.stat().st_size / (1024 * 1024)

        # Delete the file
        backup_path.unlink()
        
        # Log deletion
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="backup.delete",
            resource_type="system",
            resource_id=None,
            details={
                "backup_filename": backup_filename,
                "size_mb": round(file_size_mb, 2)
            }
        )
        
        return {
            "message": f"Backup file '{backup_filename}' deleted successfully",
            "size_freed_mb": round(file_size_mb, 2)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete backup: {str(e)}")


@router.get("/recommendations")
def get_backup_recommendations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Get backup configuration recommendations"""
    return backup_scheduler.get_backup_recommendations()


@router.get("/health")
def backup_health_check(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Check backup system health"""
    try:
        status = backup_scheduler.get_status()
        health = {
            "status": "healthy",
            "issues": []
        }
        
        # Check if backups are enabled
        if not status["enabled"]:
            health["issues"].append({
                "severity": "warning",
                "message": "Automated backups are disabled",
                "recommendation": "Enable automated backups for data protection"
            })
        
        # Check if scheduler is running when it should be
        if status["enabled"] and not status["running"]:
            health["issues"].append({
                "severity": "error",
                "message": "Backup scheduler is not running",
                "recommendation": "Start the backup scheduler service"
            })
            health["status"] = "unhealthy"
        
        # Check recent backup success
        recent_backups = status.get("backup_history", [])
        if recent_backups:
            last_backup = recent_backups[-1]
            if last_backup.get("status") != "success":
                health["issues"].append({
                    "severity": "error",
                    "message": "Last backup failed",
                    "recommendation": "Check backup logs and fix issues"
                })
                health["status"] = "unhealthy"
        elif status["enabled"]:
            health["issues"].append({
                "severity": "warning",
                "message": "No backup history found",
                "recommendation": "Ensure backups are running as scheduled"
            })
        
        # Check disk space
        backup_path = Path(status["configuration"]["backup_path"])
        if backup_path.exists():
            import shutil
            total, used, free = shutil.disk_usage(backup_path)
            free_gb = free / (1024**3)
            
            if free_gb < 1:  # Less than 1GB
                health["issues"].append({
                    "severity": "error",
                    "message": f"Very low disk space for backups: {free_gb:.1f}GB",
                    "recommendation": "Free up disk space or change backup location"
                })
                health["status"] = "unhealthy"
            elif free_gb < 5:  # Less than 5GB
                health["issues"].append({
                    "severity": "warning",
                    "message": f"Low disk space for backups: {free_gb:.1f}GB",
                    "recommendation": "Consider freeing up disk space"
                })
                if health["status"] == "healthy":
                    health["status"] = "warning"
        
        if not health["issues"]:
            health["issues"].append({
                "severity": "info",
                "message": "Backup system is healthy",
                "recommendation": "Continue monitoring backup operations"
            })
        
        return health
    
    except Exception as e:
        return {
            "status": "error",
            "issues": [{
                "severity": "error",
                "message": f"Health check failed: {str(e)}",
                "recommendation": "Check backup system configuration"
            }]
        }