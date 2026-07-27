"""
Automated backup scheduling service.
"""
import schedule
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path
from loguru import logger

from ..database import get_db
from ..utils.backup import create_backup, list_backups
from ..models import User


class BackupScheduler:
    """
    Automated backup scheduling service with configurable intervals.
    """
    
    def __init__(self):
        self.running = False
        self.scheduler_thread: Optional[threading.Thread] = None
        self.backup_config = {
            "enabled": False,
            "interval_hours": 24,  # Default: daily backups
            "max_backups": 7,      # Keep last 7 backups
            "include_files": True,
            "backup_path": "data/backups"
        }
        self.last_backup: Optional[datetime] = None
        self.backup_history = []
    
    def configure(
        self,
        enabled: bool = True,
        interval_hours: int = 24,
        max_backups: int = 7,
        include_files: bool = True,
        backup_path: str = "data/backups"
    ):
        """
        Configure backup scheduler settings.
        
        Args:
            enabled: Whether automatic backups are enabled
            interval_hours: Hours between backups
            max_backups: Maximum number of backups to keep
            include_files: Whether to include document files
            backup_path: Directory to store backups
        """
        self.backup_config.update({
            "enabled": enabled,
            "interval_hours": interval_hours,
            "max_backups": max_backups,
            "include_files": include_files,
            "backup_path": backup_path
        })
        
        logger.info(
            f"Backup scheduler configured: enabled={enabled}, "
            f"interval={interval_hours}h, max_backups={max_backups}"
        )
        
        # Restart scheduler with new configuration
        if self.running:
            self.stop()
            self.start()
    
    def start(self):
        """Start the backup scheduler."""
        if self.running:
            logger.warning("Backup scheduler is already running")
            return
        
        if not self.backup_config["enabled"]:
            logger.info("Backup scheduler is disabled")
            return
        
        self.running = True
        
        # Clear existing scheduled jobs
        schedule.clear()
        
        # Schedule backup job
        interval_hours = self.backup_config["interval_hours"]
        schedule.every(interval_hours).hours.do(self._run_scheduled_backup)
        
        # Start scheduler thread
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        
        logger.info(f"Backup scheduler started with {interval_hours} hour interval")
    
    def stop(self):
        """Stop the backup scheduler."""
        self.running = False
        schedule.clear()
        
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)
        
        logger.info("Backup scheduler stopped")
    
    def _scheduler_loop(self):
        """Main scheduler loop."""
        while self.running:
            try:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Error in backup scheduler loop: {e}")
                time.sleep(300)  # Wait 5 minutes on error
    
    def _run_scheduled_backup(self):
        """Run a scheduled backup."""
        if not self.running or not self.backup_config["enabled"]:
            return
        
        logger.info("Starting scheduled backup")
        
        try:
            # Create backup
            backup_info = self.create_backup()
            
            if backup_info and not backup_info.get('errors'):
                self.last_backup = datetime.utcnow()
                self.backup_history.append({
                    "timestamp": self.last_backup.isoformat(),
                    "backup_name": backup_info["name"],
                    "size_mb": backup_info.get("archive_size_mb", 0),
                    "status": "success"
                })
                
                logger.info(f"Scheduled backup completed: {backup_info['name']}")
                
                # Clean up old backups
                self._cleanup_old_backups()
                
            else:
                # Log backup failure
                self.backup_history.append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "status": "failed",
                    "errors": backup_info.get('errors', ['Unknown error']) if backup_info else ['Backup creation failed']
                })
                
                logger.error(f"Scheduled backup failed: {backup_info.get('errors') if backup_info else 'Unknown error'}")
        
        except Exception as e:
            logger.error(f"Error during scheduled backup: {e}")
            self.backup_history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "status": "failed",
                "errors": [str(e)]
            })
    
    def create_backup(self, custom_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Create a backup manually or via scheduler.
        
        Args:
            custom_name: Optional custom backup name
            
        Returns:
            Backup information dictionary
        """
        try:
            with next(get_db()) as db:
                # Create system user for automated backups
                system_user = User(
                    username="system",
                    full_name="Automated Backup System",
                    is_admin=True,
                    is_active=True
                )
                
                backup_name = custom_name or f"scheduled_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                
                backup_info = create_backup(
                    db_session=db,
                    backup_name=backup_name,
                    include_files=self.backup_config["include_files"],
                    user=system_user
                )
                
                return backup_info
                
        except Exception as e:
            logger.error(f"Error creating backup: {e}")
            return {"errors": [str(e)]}
    
    def _cleanup_old_backups(self):
        """Remove old backups to maintain the configured limit."""
        try:
            backup_path = Path(self.backup_config["backup_path"])
            if not backup_path.exists():
                return
            
            # Get list of backup files
            backup_files = list(backup_path.glob("*.tar.gz"))
            
            # Sort by creation time (newest first)
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            # Remove excess backups
            max_backups = self.backup_config["max_backups"]
            if len(backup_files) > max_backups:
                for old_backup in backup_files[max_backups:]:
                    try:
                        old_backup.unlink()
                        logger.info(f"Removed old backup: {old_backup.name}")
                    except Exception as e:
                        logger.error(f"Failed to remove old backup {old_backup}: {e}")
        
        except Exception as e:
            logger.error(f"Error during backup cleanup: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get backup scheduler status and statistics.
        
        Returns:
            Status information dictionary
        """
        return {
            "enabled": self.backup_config["enabled"],
            "running": self.running,
            "configuration": self.backup_config,
            "last_backup": self.last_backup.isoformat() if self.last_backup else None,
            "next_backup": self._get_next_backup_time(),
            "backup_history": self.backup_history[-10:],  # Last 10 backups
            "available_backups": self._get_available_backups_info()
        }
    
    def _get_next_backup_time(self) -> Optional[str]:
        """Calculate next scheduled backup time."""
        if not self.backup_config["enabled"] or not self.last_backup:
            return None
        
        next_backup = self.last_backup + timedelta(hours=self.backup_config["interval_hours"])
        return next_backup.isoformat()
    
    def _get_available_backups_info(self) -> Dict[str, Any]:
        """Get information about available backup files."""
        try:
            backups = list_backups(Path(self.backup_config["backup_path"]))
            
            total_size_mb = sum(backup.get("size_mb", 0) for backup in backups)
            
            return {
                "count": len(backups),
                "total_size_mb": round(total_size_mb, 2),
                "oldest": backups[-1].get("created_at") if backups else None,
                "newest": backups[0].get("created_at") if backups else None
            }
        
        except Exception as e:
            logger.error(f"Error getting backup info: {e}")
            return {"error": str(e)}
    
    def force_backup(self, include_files: Optional[bool] = None) -> Dict[str, Any]:
        """
        Force an immediate backup outside the normal schedule.
        
        Args:
            include_files: Override file inclusion setting
            
        Returns:
            Backup result information
        """
        logger.info("Starting forced backup")
        
        try:
            # Temporarily override file inclusion if specified
            original_include_files = self.backup_config["include_files"]
            if include_files is not None:
                self.backup_config["include_files"] = include_files
            
            backup_info = self.create_backup(f"manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            
            # Restore original setting
            self.backup_config["include_files"] = original_include_files
            
            if backup_info and not backup_info.get('errors'):
                logger.info(f"Forced backup completed: {backup_info['name']}")
                
                # Update history
                self.backup_history.append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "backup_name": backup_info["name"],
                    "size_mb": backup_info.get("archive_size_mb", 0),
                    "status": "success",
                    "type": "manual"
                })
            
            return backup_info or {"errors": ["Backup creation failed"]}
        
        except Exception as e:
            logger.error(f"Error during forced backup: {e}")
            return {"errors": [str(e)]}
    
    def get_backup_recommendations(self) -> Dict[str, Any]:
        """
        Get backup recommendations based on system usage.
        
        Returns:
            Recommendations dictionary
        """
        recommendations = {
            "current_config": self.backup_config.copy(),
            "suggestions": []
        }
        
        try:
            # Analyze backup history
            recent_backups = [
                b for b in self.backup_history 
                if datetime.fromisoformat(b["timestamp"]) > datetime.utcnow() - timedelta(days=7)
            ]
            
            success_rate = (
                len([b for b in recent_backups if b["status"] == "success"]) / 
                len(recent_backups) if recent_backups else 1
            )
            
            # Check available disk space
            backup_path = Path(self.backup_config["backup_path"])
            if backup_path.exists():
                import shutil
                total, used, free = shutil.disk_usage(backup_path)
                free_gb = free / (1024**3)
                
                if free_gb < 5:  # Less than 5GB free
                    recommendations["suggestions"].append({
                        "type": "warning",
                        "message": f"Low disk space for backups: {free_gb:.1f}GB remaining",
                        "action": "Consider reducing backup retention or freeing up space"
                    })
            
            # Success rate recommendations
            if success_rate < 0.8:
                recommendations["suggestions"].append({
                    "type": "error",
                    "message": f"Backup success rate is low: {success_rate*100:.1f}%",
                    "action": "Check system logs and fix backup issues"
                })
            elif success_rate < 0.95:
                recommendations["suggestions"].append({
                    "type": "warning",
                    "message": f"Backup success rate could be improved: {success_rate*100:.1f}%",
                    "action": "Monitor backup logs for occasional failures"
                })
            
            # Backup frequency recommendations
            if self.backup_config["interval_hours"] > 72:  # More than 3 days
                recommendations["suggestions"].append({
                    "type": "info",
                    "message": "Backup interval is quite long",
                    "action": "Consider more frequent backups for better data protection"
                })
            
            # File inclusion recommendations
            if not self.backup_config["include_files"]:
                recommendations["suggestions"].append({
                    "type": "warning",
                    "message": "File backups are disabled",
                    "action": "Enable file backups for complete data protection"
                })
            
            if not recommendations["suggestions"]:
                recommendations["suggestions"].append({
                    "type": "success",
                    "message": "Backup configuration looks good",
                    "action": "Continue monitoring backup health"
                })
        
        except Exception as e:
            logger.error(f"Error generating backup recommendations: {e}")
            recommendations["error"] = str(e)
        
        return recommendations


# Global backup scheduler instance
backup_scheduler = BackupScheduler()
