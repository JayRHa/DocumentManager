import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
from sqlalchemy.orm import Session
from collections import defaultdict
from datetime import datetime, timedelta

from ..config import get_settings
from ..database import SessionLocal
from .document_processor import DocumentProcessor

class FileWatcherHandler(FileSystemEventHandler):
    """Handler for file system events"""
    
    def __init__(self, settings):
        self.settings = settings
        # Track recently processed files to prevent duplicates
        self._recent_files = defaultdict(datetime)
        self._debounce_seconds = 5
    
    def on_created(self, event):
        """Handle file creation events"""
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Check if file extension is allowed
        if file_path.suffix.lower() not in [f".{ext}" for ext in self.settings.allowed_extensions_list]:
            logger.info(f"Ignoring file with unsupported extension: {file_path.name}")
            return
        
        # Check if this file was recently processed (debounce)
        file_key = str(file_path.absolute())
        last_processed = self._recent_files.get(file_key)
        now = datetime.now()
        
        if last_processed and (now - last_processed).total_seconds() < self._debounce_seconds:
            logger.info(f"Skipping file {file_path.name} - recently processed {(now - last_processed).total_seconds():.1f}s ago")
            return
        
        # Update last processed time
        self._recent_files[file_key] = now
        
        # Clean up old entries
        cutoff_time = now - timedelta(seconds=self._debounce_seconds * 2)
        self._recent_files = defaultdict(datetime, {k: v for k, v in self._recent_files.items() if v > cutoff_time})
        
        # Wait a bit to ensure file is completely written
        time.sleep(2)
        
        # Check if file still exists (might have been moved/deleted)
        if not file_path.exists():
            logger.info(f"File disappeared after creation event: {file_path.name}")
            return
        
        logger.info(f"New file detected: {file_path.name}")
        
        # Create the processor with the same live session used for this job.
        # Keeping a startup-time processor leaked closed SQLAlchemy sessions
        # into later AI/settings operations.
        try:
            with SessionLocal() as db:
                processor = DocumentProcessor(db)
                result = processor.process_file(file_path, db)
                if result:
                    logger.info(f"Successfully processed file: {file_path.name}")
                else:
                    logger.warning(f"Processing returned no result for: {file_path.name}")
        except Exception as e:
            logger.error(f"Failed to process file {file_path.name}: {e}")
            # Check if file still exists, if not, it might have been deleted during processing
            if not file_path.exists():
                logger.info(f"File was deleted during processing: {file_path.name}")
    
    def on_moved(self, event):
        """Handle file move events"""
        if event.is_directory:
            return
        
        dest_path = Path(event.dest_path)
        
        # Check if moved into staging folder
        if dest_path.parent.name == Path(self.settings.staging_folder).name:
            self.on_created(type('Event', (), {'src_path': event.dest_path, 'is_directory': False})())

class FileWatcher:
    """File system watcher for the staging folder"""
    
    def __init__(self, db: Session = None):
        self.settings = None
        self.observer = Observer()
        self.is_running = False
        self._initialized = False
    
    def _ensure_initialized(self):
        """Ensure the file watcher is initialized with database settings"""
        if not self._initialized:
            try:
                with SessionLocal() as db:
                    self.settings = get_settings(db)
                self._initialized = True
            except Exception as e:
                logger.error(f"Failed to initialize file watcher: {e}")
                raise
    
    def start(self):
        """Start watching the staging folder"""
        self._ensure_initialized()
        staging_path = Path(self.settings.staging_folder)
        
        if not staging_path.exists():
            logger.warning(f"Staging folder does not exist: {staging_path}")
            return
        
        handler = FileWatcherHandler(self.settings)
        self.observer.schedule(handler, str(staging_path), recursive=False)
        
        try:
            self.observer.start()
            self.is_running = True
            logger.info(f"Started file watcher for: {staging_path}")
            
            # Process any existing files in staging folder
            self._process_existing_files()
            
        except Exception as e:
            logger.error(f"Failed to start file watcher: {e}")
            self.is_running = False
    
    def stop(self):
        """Stop the file watcher"""
        if self.is_running and self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.is_running = False
            logger.info("Stopped file watcher")
    
    def _process_existing_files(self):
        """Process any files that already exist in the staging folder"""
        self._ensure_initialized()
        staging_path = Path(self.settings.staging_folder)
        
        try:
            for file_path in staging_path.iterdir():
                if file_path.is_file():
                    # Double-check file still exists before processing
                    if not file_path.exists():
                        logger.info(f"File disappeared before processing: {file_path.name}")
                        continue
                    
                    # Check if file extension is allowed
                    if file_path.suffix.lower() in [f".{ext}" for ext in self.settings.allowed_extensions_list]:
                        logger.info(f"Processing existing file: {file_path.name}")
                        
                        try:
                            with SessionLocal() as db:
                                processor = DocumentProcessor(db)
                                processor.process_file(file_path, db)
                        except Exception as e:
                            logger.error(f"Failed to process existing file {file_path.name}: {e}")
        
        except Exception as e:
            logger.error(f"Failed to process existing files: {e}")
    
    def scan_and_process(self):
        """Manually scan and process all files in staging (for batch processing)"""
        logger.info("Starting manual scan of staging folder")
        self._process_existing_files()
        logger.info("Manual scan completed")
    
    @property
    def status(self) -> dict:
        """Get the current status of the file watcher"""
        if self._initialized:
            return {
                "is_running": self.is_running,
                "staging_folder": self.settings.staging_folder,
                "allowed_extensions": self.settings.allowed_extensions_list
            }
        else:
            return {
                "is_running": self.is_running,
                "staging_folder": "Not initialized",
                "allowed_extensions": []
            }
