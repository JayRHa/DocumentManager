from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from loguru import logger
import os
from .config import get_settings

# Honour the documented DATABASE_URL runtime setting.
DATABASE_URL = get_settings().database_url

# Ensure data directory exists
os.makedirs("data", exist_ok=True)

# Create engine with minimal configuration
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class
Base = declarative_base()

def get_db() -> Session:
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize database tables"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully")
        
        # Initialize default settings if needed
        from .utils.init_settings import initialize_default_settings
        # Initialize default document types
        from .services.doctype_manager import ensure_default_document_types
        
        db = SessionLocal()
        try:
            # Initialize default settings
            created_settings = initialize_default_settings(db)
            if created_settings:
                logger.info(f"Initialized {len(created_settings)} default settings in database")
            
            # Initialize default document types
            ensure_default_document_types(db)
            logger.info("Initialized default document types in database")
            
        except Exception as e:
            logger.warning(f"Could not initialize defaults: {e}")
            db.rollback()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
