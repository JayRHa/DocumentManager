from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session
import os
import uvicorn
from pathlib import Path

from .database import get_db
from .routers import search, settings, doctypes, tags, health, auth, backup, documents, correspondents, security, admin_fix
from .services.backup_scheduler import backup_scheduler
# from .middleware.auth_middleware import AuthMiddleware  # Disabled for now
from .middleware.error_handler import ErrorHandler
from .config import get_settings
# Temporarily disable complex middleware for fast startup
from .middleware.csrf_middleware import CSRFProtect
from .middleware.rate_limit_middleware import RateLimitProtect
# from .middleware.logging_middleware import LoggingMiddleware, RequestContextMiddleware
# from .utils.logging_config import configure_application_logging, log_security_event
# from loguru import logger

# Tables will be created when needed

app = FastAPI(
    title="Document Management System",
    description="A comprehensive document management system with OCR and AI-powered search",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:8000"],  # Restrict origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],  # Restrict headers
)

# Add global exception handlers
app.add_exception_handler(HTTPException, ErrorHandler.http_exception_handler)
app.add_exception_handler(RequestValidationError, ErrorHandler.validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, ErrorHandler.starlette_exception_handler)
app.add_exception_handler(Exception, ErrorHandler.general_exception_handler)

# Get settings to determine production mode
app_settings = get_settings()

# Enable CSRF protection
csrf_protect = CSRFProtect(
    secure=app_settings.production_mode,  # Use secure cookies in production
    exclude_paths={
        "/api/auth/login",
        "/api/auth/logout", 
        "/api/auth/check-session",
        "/api/auth/setup/check",
        "/api/auth/setup/initial-user",
        "/api/health",
        "/api/settings/test/ai",  # Exclude the original test endpoint
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/csrf-token"
    }
)
csrf_protect.init_app(app)

# Enable rate limiting
rate_limit = RateLimitProtect(
    default_limit=100,  # 100 requests per minute for general endpoints
    window_seconds=60,
    login_limit=5,  # 5 login attempts per 5 minutes
    login_window_seconds=300
)
rate_limit.init_app(app)

# Authentication middleware (disabled for now)
# app.add_middleware(AuthMiddleware)

# Include routers
app.include_router(auth.router, prefix="/api/auth", tags=["authentication"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(doctypes.router, prefix="/api/doctypes", tags=["doctypes"])
app.include_router(tags.router, prefix="/api/tags", tags=["tags"])
app.include_router(health.router, prefix="/api/health", tags=["health"])
app.include_router(backup.router, prefix="/api/backup", tags=["backup"])
app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
app.include_router(correspondents.router, prefix="/api/correspondents", tags=["correspondents"])
app.include_router(security.router, prefix="/api/security", tags=["security"])
app.include_router(admin_fix.router, prefix="/api/admin", tags=["admin"])

# Serve static files
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")

# Global file watcher instance
file_watcher = None

# Removed vector database check for faster startup

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup"""
    global file_watcher
    
    print("🚀 Document Management System starting...")
    print("📊 All initialization will happen on first access")
    print("⚡ Fast startup mode - no database operations")
    
    # Initialize backup scheduler (but don't start it automatically)
    try:
        backup_scheduler.configure(enabled=False)  # Disabled by default
        print("📦 Backup scheduler initialized (disabled by default)")
    except Exception as e:
        print(f"⚠️  Could not initialize backup scheduler: {e}")
    
    # Schedule default document types check in background
    import asyncio
    from .database import SessionLocal
    from .services.doctype_manager import ensure_default_document_types
    
    async def ensure_defaults():
        await asyncio.sleep(2)  # Wait 2 seconds to not impact startup
        # First ensure database tables exist
        from .database import init_db
        try:
            init_db()
            print("✅ Database tables initialized")
        except Exception as e:
            print(f"⚠️  Could not initialize database: {e}")
            return
            
        db = SessionLocal()
        try:
            # Ensure folders are created in the right location
            from .services.folder_setup import setup_folders
            setup_folders(db)
            print("✅ Folder structure initialized")
            
            ensure_default_document_types(db)
            print("✅ Default document types ensured")
        except Exception as e:
            print(f"⚠️  Could not ensure default document types: {e}")
        finally:
            db.close()
    
    # Run in background without blocking startup
    asyncio.create_task(ensure_defaults())
    
    # Initialize file watcher for automatic document processing
    async def start_file_watcher():
        await asyncio.sleep(3)  # Wait for database initialization
        try:
            from .services.file_watcher import FileWatcherHandler
            from .services.document_processor import DocumentProcessor
            from watchdog.observers import Observer
            
            db = SessionLocal()
            try:
                settings = get_settings(db)
                staging_path = Path(settings.staging_folder)
                
                # Ensure staging folder exists
                staging_path.mkdir(parents=True, exist_ok=True)
                
                # Create document processor and file watcher
                processor = DocumentProcessor(db)
                event_handler = FileWatcherHandler(processor, settings, db)
                
                # Set up observer
                global file_watcher
                file_watcher = Observer()
                file_watcher.schedule(event_handler, str(staging_path), recursive=False)
                file_watcher.start()
                
                print(f"📁 File watcher started monitoring: {staging_path}")
                
            finally:
                db.close()
                
        except Exception as e:
            print(f"⚠️  Could not start file watcher: {e}")
    
    # Start file watcher in background
    asyncio.create_task(start_file_watcher())

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on application shutdown"""
    global file_watcher
    if file_watcher:
        file_watcher.stop()
    
    # Stop backup scheduler
    try:
        backup_scheduler.stop()
        print("📦 Backup scheduler stopped")
    except Exception as e:
        print(f"⚠️  Error stopping backup scheduler: {e}")

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the login page"""
    login_path = Path(__file__).parent.parent / "frontend" / "login.html"
    if login_path.exists():
        return FileResponse(str(login_path))
    return HTMLResponse("""
    <html>
        <head><title>Login - Document Management System</title></head>
        <body>
            <h1>Login Required</h1>
            <p>Please configure authentication.</p>
        </body>
    </html>
    """)

@app.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    db: Session = Depends(get_db)
):
    """Serve the main application page"""
    # Check if user is authenticated via session
    from .services.auth_service import get_user_from_session_token
    user = get_user_from_session_token(request, db)
    
    if not user:
        # Redirect to login if not authenticated
        return RedirectResponse(url="/login", status_code=302)
    
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend_path.exists():
        return FileResponse(str(frontend_path))
    return HTMLResponse("""
    <html>
        <head><title>Document Management System</title></head>
        <body>
            <h1>Document Management System</h1>
            <p>API is running. Frontend files not found.</p>
            <p>Visit <a href="/docs">/docs</a> for API documentation.</p>
        </body>
    </html>
    """)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "message": "Document Management System is running"}

@app.get("/api/health")
async def api_health_check():
    """API health check endpoint - redirects to full health check"""
    return {"status": "healthy", "message": "Document Management System is running", "note": "For detailed health check, use /api/health/"}

@app.get("/{path:path}")
async def catch_all(request: Request, path: str):
    """Catch-all route for undefined paths"""
    # For API paths, return JSON 404
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    
    # For other paths, return HTML 404 page
    return ErrorHandler.create_error_page(
        status_code=404,
        title="Page Not Found",
        message="The page you're looking for doesn't exist.",
        request=request
    )

if __name__ == "__main__":
    host = os.getenv("DOCUMENT_MANAGER_HOST", "127.0.0.1")
    uvicorn.run("app.main:app", host=host, port=8000, reload=True)
