from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Dict, Any
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

from ..database import get_db
from ..models import Settings as SettingsModel, User
from ..schemas import ExtendedSettingsResponse, ExtendedSettingsUpdate, ExportConfigResponse, AIProviderConfig
from pydantic import BaseModel
from ..config import get_settings, reset_settings
from ..services.ai_client_factory import AIClientFactory
from ..services.auth_service import require_permission_flexible, require_admin_flexible
from ..utils.init_settings import initialize_default_settings

# Define AIProviderStatus if not imported
class AIProviderStatus(BaseModel):
    provider: str
    is_configured: bool
    status_message: str
    models: Dict[str, str]

router = APIRouter()

@router.get("/health/")
def health_check(db: Session = Depends(get_db)):
    """Health check endpoint for settings"""
    try:
        # Try to query settings to ensure DB connection works
        settings_count = db.query(SettingsModel).count()
        return {
            "status": "healthy",
            "settings_count": settings_count,
            "message": "Settings service is operational"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "message": "Settings service has issues"
        }

@router.get("/debug/azure")
def debug_azure_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Debug endpoint to check Azure settings (admin only).

    Even though the Azure API key is masked, this endpoint dumps the full
    Azure deployment configuration (endpoint, deployment names) and other
    raw settings, so it must require admin authentication.
    """
    settings = get_settings(db)
    db_settings = db.query(SettingsModel).filter(
        SettingsModel.key.in_(['ai_provider', 'azure_openai_api_key', 'azure_openai_endpoint',
                               'azure_openai_chat_deployment', 'azure_openai_embeddings_deployment'])
    ).all()

    # Mask any sensitive values in db_settings as well so we don't leak the
    # raw api key from the settings table even to admins via this endpoint.
    sensitive_keys = {"azure_openai_api_key", "openai_api_key", "jwt_secret_key", "secret_key"}
    masked_db_settings = {
        s.key: ("***" if s.key in sensitive_keys and s.value else s.value)
        for s in db_settings
    }
    return {
        "loaded_settings": {
            "ai_provider": settings.ai_provider,
            "azure_api_key": "***" if settings.azure_openai_api_key else None,
            "azure_endpoint": settings.azure_openai_endpoint,
            "azure_chat_deployment": settings.azure_openai_chat_deployment,
            "azure_embeddings_deployment": settings.azure_openai_embeddings_deployment
        },
        "db_settings": masked_db_settings,
    }


def save_setting_to_db(db: Session, key: str, value: str, description: str = None):
    """Save or update a setting in the database"""
    setting = db.query(SettingsModel).filter(SettingsModel.key == key).first()
    
    if setting:
        setting.value = value
        if description:
            setting.description = description
    else:
        setting = SettingsModel(
            key=key,
            value=value,
            description=description
        )
        db.add(setting)
    
    db.commit()
    
    # Reset settings to reload from database
    reset_settings()

@router.get("/")
def get_all_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Get all settings from database"""
    settings = db.query(SettingsModel).all()
    return settings

@router.get("/setup-config")
def get_setup_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Get current configuration for setup wizard (admin only).

    This endpoint returns OpenAI and Azure OpenAI API keys in cleartext, so
    it must require admin authentication.
    """
    try:
        config = get_settings(db)

        return {
            "ai_provider": config.ai_provider,
            "openai_api_key": config.openai_api_key if config.openai_api_key else "",
            "azure_openai_api_key": config.azure_openai_api_key if config.azure_openai_api_key else "",
            "azure_openai_endpoint": config.azure_openai_endpoint,
            "azure_openai_chat_deployment": config.azure_openai_chat_deployment,
            "azure_openai_embeddings_deployment": config.azure_openai_embeddings_deployment,
            "embedding_model": config.embedding_model,
            "analysis_model": config.analysis_model,
            "chat_model": config.chat_model,
            "root_folder": config.root_folder if config.root_folder else str(Path.cwd()),
            "staging_folder": config.staging_folder,
            "storage_folder": config.storage_folder,
            "data_folder": config.data_folder
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/save-config")
def save_configuration(
    config_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Save configuration from setup wizard to database"""
    try:
        # Map of frontend keys to backend settings keys
        key_mapping = {
            "ai_provider": "ai_provider",
            "openai_api_key": "openai_api_key",
            "azure_openai_api_key": "azure_openai_api_key",
            "azure_openai_endpoint": "azure_openai_endpoint",
            "azure_openai_chat_deployment": "azure_openai_chat_deployment",
            "azure_openai_embeddings_deployment": "azure_openai_embeddings_deployment",
            "embedding_model": "embedding_model",
            "analysis_model": "analysis_model",
            "chat_model": "chat_model",
            "root_folder": "root_folder",
            "staging_folder": "staging_folder",
            "storage_folder": "storage_folder",
            "data_folder": "data_folder"
        }
        
        # Save each setting to database
        for frontend_key, backend_key in key_mapping.items():
            if frontend_key in config_data:
                value = config_data[frontend_key]
                if value is not None and str(value).strip():
                    save_setting_to_db(db, backend_key, str(value))
        
        # Reset settings to reload from database
        reset_settings()
        
        return {"message": "Configuration saved successfully", "restart_required": False}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/extended")
def get_extended_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
) -> ExtendedSettingsResponse:
    """Get all settings with additional computed values (admin only).

    Previously required only the ``settings.read`` permission, which the
    default ``editor`` role grants. That meant any non-admin editor could
    read the cleartext OpenAI/Azure API keys and the ``secret_key`` via
    this endpoint. Restricted to admins to fix the privilege-escalation
    path.
    """
    try:
        config = get_settings(db)
        
        return ExtendedSettingsResponse(
            openai_api_key=config.openai_api_key if config.openai_api_key else "",
            root_folder=config.root_folder if config.root_folder else str(Path.cwd()),
            staging_folder=config.staging_folder,
            storage_folder=config.storage_folder,
            data_folder=config.data_folder,
            logs_folder=config.logs_folder,
            max_file_size=config.max_file_size,
            allowed_extensions=config.allowed_extensions,
            log_level=config.log_level,
            tesseract_path=config.tesseract_path,
            poppler_path=config.poppler_path,
            ai_text_limit=config.ai_text_limit,
            ai_context_limit=config.ai_context_limit,
            ai_provider=config.ai_provider,
            embedding_model=config.embedding_model,
            chat_model=config.chat_model,
            analysis_model=config.analysis_model,
            azure_openai_api_key=config.azure_openai_api_key if config.azure_openai_api_key else "",
            azure_openai_endpoint=config.azure_openai_endpoint,
            azure_openai_api_version=config.azure_openai_api_version,
            azure_openai_chat_deployment=config.azure_openai_chat_deployment,
            azure_openai_embeddings_deployment=config.azure_openai_embeddings_deployment,
            # Add missing required fields
            database_url=config.database_url,
            chroma_host=config.chroma_host,
            chroma_port=config.chroma_port,
            chroma_collection_name=config.chroma_collection_name,
            secret_key=config.secret_key,
            algorithm=config.algorithm,
            access_token_expire_minutes=config.access_token_expire_minutes
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/extended")
@router.post("/extended")
def update_extended_settings(
    updates: ExtendedSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
) -> Dict[str, Any]:
    """Update multiple settings at once"""
    from loguru import logger
    
    try:
        update_dict = updates.dict(exclude_unset=True)
        
        logger.info(f"Extended settings update received: {list(update_dict.keys())}")
        
        if not update_dict:
            return {"message": "No updates provided"}
        
        # Log API key updates specifically
        if 'openai_api_key' in update_dict:
            key_value = update_dict['openai_api_key']
            logger.info(f"OpenAI API key in update: length={len(key_value) if key_value else 0}, empty={not key_value}")
        
        # Save each non-None setting to database
        for key, value in update_dict.items():
            if value is not None:
                # Skip empty API keys to avoid overwriting existing ones
                if key.endswith('_api_key') and not value:
                    logger.info(f"Skipping empty API key update for {key}")
                    continue
                save_setting_to_db(db, key, str(value))
        
        # Reset settings to reload from database
        reset_settings()
        
        # Create folders if paths were updated
        if any(key in update_dict for key in ["root_folder", "staging_folder", "storage_folder", "data_folder", "logs_folder"]):
            from ..services.folder_setup import setup_folders
            setup_folders()
        
        return {
            "message": "Settings updated successfully",
            "updated_fields": list(update_dict.keys()),
            "restart_required": False
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/ai-provider/status")
def get_ai_provider_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
) -> AIProviderStatus:
    """Get current AI provider configuration and test connection (admin only).

    Discloses which AI provider is configured, deployment/model names, and
    actively attempts an outbound API call. Restricted to admins to avoid
    information disclosure and unauthenticated outbound traffic.
    """
    try:
        config = get_settings(db)
        
        # Basic configuration check
        is_configured = False
        status_message = "Not configured"
        
        if config.ai_provider == "openai":
            if config.openai_api_key:
                is_configured = True
                status_message = "OpenAI API key configured"
                
                # Try to test connection if configured
                try:
                    ai_factory = AIClientFactory()
                    client = ai_factory.create_client(db)
                    
                    # Test with a minimal completion
                    client.chat.completions.create(
                        model=config.chat_model,
                        messages=[{"role": "user", "content": "Hi"}],
                        max_tokens=5
                    )
                    status_message = "OpenAI connection successful"
                except Exception as e:
                    status_message = f"OpenAI configured but connection failed: {str(e)}"
            else:
                status_message = "OpenAI API key not set"
                
        elif config.ai_provider == "azure":
            if config.azure_openai_api_key and config.azure_openai_endpoint:
                is_configured = True
                status_message = "Azure OpenAI configured"
                
                # Try to test connection if configured
                try:
                    ai_factory = AIClientFactory()
                    client = ai_factory.create_client(db)
                    
                    # Test with deployment name
                    if config.azure_openai_chat_deployment:
                        client.chat.completions.create(
                            model=config.azure_openai_chat_deployment,
                            messages=[{"role": "user", "content": "Hi"}],
         
                        )
                        status_message = "Azure OpenAI connection successful"
                    else:
                        status_message = "Azure OpenAI configured but chat deployment not set"
                except Exception as e:
                    status_message = f"Azure configured but connection failed: {str(e)}"
            else:
                missing = []
                if not config.azure_openai_api_key:
                    missing.append("API key")
                if not config.azure_openai_endpoint:
                    missing.append("endpoint")
                status_message = f"Azure OpenAI missing: {', '.join(missing)}"
        
        return AIProviderStatus(
            provider=config.ai_provider,
            is_configured=is_configured,
            status_message=status_message,
            models={
                "chat": config.chat_model if config.ai_provider == "openai" else (config.azure_openai_chat_deployment or "Not set"),
                "embeddings": config.embedding_model if config.ai_provider == "openai" else (config.azure_openai_embeddings_deployment or "Not set"),
                "analysis": config.analysis_model if config.ai_provider == "openai" else (config.azure_openai_chat_deployment or "Not set")
            }
        )
        
    except Exception as e:
        import traceback
        print(f"Error in get_ai_provider_status: {str(e)}")
        print(traceback.format_exc())
        
        return AIProviderStatus(
            provider="unknown",
            is_configured=False,
            status_message=f"Error loading configuration: {str(e)}",
            models={}
        )

@router.post("/test/ai")
def test_ai_connection(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Test AI provider connection and configuration"""
    import traceback
    from loguru import logger
    
    # Wrap everything in a try-catch to ensure we log any issues
    try:
        logger.info("Starting AI connection test")
        logger.info(f"Current user: {current_user.username if current_user else 'None'}")
        logger.info(f"User is admin: {current_user.is_admin if current_user else 'None'}")
    except Exception as e:
        logger.error(f"Error at the very start of test_ai_connection: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Startup error: {str(e)}")
    
    try:
        logger.info("Getting settings from database")
        
        # First check if the key exists in database
        from ..models import Settings as SettingsModel
        db_key = db.query(SettingsModel).filter(SettingsModel.key == "openai_api_key").first()
        logger.info(f"Database has openai_api_key: {db_key is not None}")
        if db_key:
            logger.info(f"Database key value length: {len(db_key.value) if db_key.value else 0}")
        
        try:
            config = get_settings(db)
            logger.info(f"Settings loaded successfully")
        except Exception as e:
            logger.error(f"Failed to get settings: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to load settings: {str(e)}")
            
        logger.info(f"AI provider: {config.ai_provider}, has key: {bool(config.openai_api_key)}")
        logger.info(f"Config key length: {len(config.openai_api_key) if config.openai_api_key else 0}")
        
        if config.ai_provider == "openai":
            if not config.openai_api_key:
                raise HTTPException(status_code=400, detail="OpenAI API key not configured")
                
            # Test OpenAI connection
            try:
                logger.info("Creating OpenAI client")
                client = AIClientFactory.create_client(db)
                logger.info("OpenAI client created successfully")
            except ValueError as e:
                logger.error(f"Failed to create OpenAI client: {str(e)}")
                raise HTTPException(status_code=400, detail=str(e))
            
            # Test with a simple completion
            try:
                logger.info(f"Testing chat completion with model: {config.chat_model}")
                response = client.chat.completions.create(
                    model=config.chat_model,
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=5
                )
                logger.info("OpenAI API call successful")
                return {"message": "OpenAI connection successful", "status": "ok", "model": config.chat_model}
            except Exception as e:
                logger.error(f"OpenAI API call failed: {str(e)}", exc_info=True)
                logger.error(f"Exception type: {type(e).__name__}")
                logger.error(f"Exception repr: {repr(e)}")
                
                # Get the string representation
                error_str = str(e)
                logger.error(f"Error string: '{error_str}'")
                logger.error(f"Error string type: {type(error_str)}")
                logger.error(f"Error string repr: {repr(error_str)}")
                
                # Check if it's an authentication error
                if "401" in error_str or "api_key" in error_str.lower():
                    raise HTTPException(status_code=401, detail="Invalid API key")
                elif "404" in error_str:
                    raise HTTPException(status_code=404, detail=f"Model '{config.chat_model}' not found or not accessible")
                else:
                    raise HTTPException(status_code=500, detail=f"OpenAI API error: {error_str}")
            
        elif config.ai_provider == "azure":
            if not config.azure_openai_api_key or not config.azure_openai_endpoint:
                raise HTTPException(status_code=400, detail="Azure OpenAI configuration incomplete")
                
            if not config.azure_openai_chat_deployment:
                raise HTTPException(status_code=400, detail="Azure chat deployment not configured")
                
            # Test Azure OpenAI connection
            try:
                client = AIClientFactory.create_client(db)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            
            try:
                response = client.chat.completions.create(
                    model=config.azure_openai_chat_deployment,
                    messages=[{"role": "user", "content": "Test"}],
        
                )
                return {"message": "Azure OpenAI connection successful", "status": "ok", "deployment": config.azure_openai_chat_deployment}
            except Exception as e:
                # Check if it's an authentication error
                if "401" in str(e) or "api_key" in str(e).lower():
                    raise HTTPException(status_code=401, detail="Invalid API key")
                elif "404" in str(e):
                    raise HTTPException(status_code=404, detail=f"Deployment '{config.azure_openai_chat_deployment}' not found")
                else:
                    raise HTTPException(status_code=500, detail=f"Azure OpenAI API error: {str(e)}")
            
        else:
            raise HTTPException(status_code=400, detail=f"Unknown AI provider: {config.ai_provider}")
            
    except HTTPException as he:
        # Log HTTP exceptions before re-raising
        logger.error(f"HTTP exception in test_ai_connection: {he.status_code} - {he.detail}")
        raise
    except Exception as e:
        # Log unexpected errors
        logger.error("Unexpected error in test_ai_connection:", extra={"error": str(e), "type": type(e).__name__}, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@router.post("/ai-provider/switch")
def switch_ai_provider(
    provider_config: AIProviderConfig,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Switch between AI providers and update configuration"""
    try:
        if provider_config.provider not in ["openai", "azure"]:
            raise HTTPException(status_code=400, detail="Provider must be 'openai' or 'azure'")
        
        # Save provider setting
        save_setting_to_db(db, "ai_provider", provider_config.provider)
        
        if provider_config.provider == "openai":
            # Update OpenAI settings
            if provider_config.openai_api_key:
                save_setting_to_db(db, "openai_api_key", provider_config.openai_api_key)
                
        elif provider_config.provider == "azure":
            # Update Azure OpenAI settings
            if provider_config.azure_api_key:
                save_setting_to_db(db, "azure_openai_api_key", provider_config.azure_api_key)
            if provider_config.azure_endpoint:
                save_setting_to_db(db, "azure_openai_endpoint", provider_config.azure_endpoint)
            if provider_config.azure_api_version:
                save_setting_to_db(db, "azure_openai_api_version", provider_config.azure_api_version)
            if provider_config.azure_chat_deployment:
                save_setting_to_db(db, "azure_openai_chat_deployment", provider_config.azure_chat_deployment)
            if provider_config.azure_embeddings_deployment:
                save_setting_to_db(db, "azure_openai_embeddings_deployment", provider_config.azure_embeddings_deployment)
        
        # Reset settings to reload from database
        reset_settings()
        
        return {"message": f"AI provider switched to {provider_config.provider}", "restart_required": False}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/config/openai")
def update_openai_config(
    openai_config: Dict[str, str],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Update OpenAI configuration"""
    from loguru import logger
    
    api_key = openai_config.get("api_key")
    
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    
    logger.info(f"Saving OpenAI API key, length: {len(api_key)}")
    
    # Store in database
    save_setting_to_db(db, "openai_api_key", api_key, "OpenAI API key for document processing")
    
    # Verify it was saved
    saved = db.query(SettingsModel).filter(SettingsModel.key == "openai_api_key").first()
    if saved:
        logger.info(f"Key saved successfully, db length: {len(saved.value) if saved.value else 0}")
    else:
        logger.error("Key not found in database after save!")
    
    return {"message": "OpenAI configuration updated successfully", "key_length": len(api_key)}

@router.post("/config/ai-limits")
def update_ai_limits(
    ai_limits: Dict[str, int],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Update AI service limits (admin only)."""
    text_limit = ai_limits.get("text_limit")
    context_limit = ai_limits.get("context_limit")
    
    if text_limit is not None:
        if text_limit < 1000 or text_limit > 100000:
            raise HTTPException(status_code=400, detail="Text limit must be between 1,000 and 100,000 characters")
        
        save_setting_to_db(db, "ai_text_limit", str(text_limit), "Maximum text length for AI document analysis")
    
    if context_limit is not None:
        if context_limit < 1000 or context_limit > 100000:
            raise HTTPException(status_code=400, detail="Context limit must be between 1,000 and 100,000 characters")
        
        save_setting_to_db(db, "ai_context_limit", str(context_limit), "Maximum context length for AI responses")
    
    return {"message": "AI limits updated successfully"}

@router.post("/config/file-settings")
def update_file_settings(
    file_settings: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Update file handling settings (admin only).

    Changing upload size limits or allowed extensions affects the system's
    security posture, so this is restricted to admins.
    """
    max_size = file_settings.get("max_file_size")
    extensions = file_settings.get("allowed_extensions")
    
    if max_size:
        save_setting_to_db(db, "max_file_size", max_size, "Maximum file size for uploads")
    
    if extensions:
        save_setting_to_db(db, "allowed_extensions", extensions, "Comma-separated list of allowed file extensions")
    
    return {"message": "File settings updated successfully"}

@router.post("/config/ocr-tools")
def update_ocr_tools(
    ocr_tools: Dict[str, str],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Update OCR tool paths (admin only).

    These paths point at executables that the server invokes; allowing
    unauthenticated callers to change them would be a remote-code-execution
    primitive.
    """
    tesseract_path = ocr_tools.get("tesseract_path")
    poppler_path = ocr_tools.get("poppler_path")
    
    if tesseract_path:
        # Verify tesseract exists
        if not Path(tesseract_path).exists():
            raise HTTPException(status_code=400, detail=f"Tesseract not found at: {tesseract_path}")
        save_setting_to_db(db, "tesseract_path", tesseract_path, "Path to Tesseract OCR binary")
    
    if poppler_path:
        # Verify poppler exists
        if not Path(poppler_path).exists():
            raise HTTPException(status_code=400, detail=f"Poppler tools not found at: {poppler_path}")
        save_setting_to_db(db, "poppler_path", poppler_path, "Path to Poppler tools directory")
    
    return {"message": "OCR tool paths updated successfully"}

@router.post("/config/folders")
def update_folder_paths(
    folders: Dict[str, str],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
):
    """Update folder paths (admin only).

    Storage/staging/data folders control where uploaded documents live;
    changing them could be used to redirect writes or hijack stored files.
    """
    updated = []
    
    for key in ["root_folder", "staging_folder", "storage_folder", "data_folder", "logs_folder"]:
        if key in folders and folders[key]:
            save_setting_to_db(db, key, folders[key])
            updated.append(key)
    
    # Create folders if they don't exist
    if updated:
        from ..services.folder_setup import setup_folders
        setup_folders()
    
    return {"message": f"Updated folders: {', '.join(updated)}", "updated": updated}

@router.get("/export")
def export_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
) -> ExportConfigResponse:
    """Export all configuration settings (admin only).

    The settings table contains the OpenAI/Azure API keys and the JWT
    secret in cleartext. Without admin authentication this endpoint
    leaks every credential the application uses.
    """
    try:
        # Get all settings from database
        db_settings = db.query(SettingsModel).all()
        settings_dict = {setting.key: setting.value for setting in db_settings}

        return ExportConfigResponse(
            version="1.0",
            exported_at=datetime.now(),
            settings=settings_dict
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/import")
async def import_configuration(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
) -> Dict[str, Any]:
    """Import configuration from JSON file (admin only).

    Without authentication, an anonymous attacker could overwrite every
    setting (including API keys, JWT secret, admin-controlled paths) by
    POSTing a crafted JSON payload.
    """
    try:
        # Read and parse JSON file
        content = await file.read()
        config_data = json.loads(content)
        
        if "settings" not in config_data:
            raise HTTPException(status_code=400, detail="Invalid configuration file format")
        
        # Import settings
        imported_count = 0
        for key, value in config_data["settings"].items():
            save_setting_to_db(db, key, value)
            imported_count += 1
        
        # Reset settings to reload from database
        reset_settings()
        
        return {
            "message": f"Successfully imported {imported_count} settings",
            "imported_count": imported_count,
            "restart_required": False
        }
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/backup")
async def backup_system(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible),
) -> Dict[str, str]:
    """Create a full system backup (admin only).

    The exported backup contains all configuration settings, including the
    cleartext OpenAI/Azure API keys and the JWT secret.
    """
    try:
        backup_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = Path("data/backups") / backup_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Export configuration
        db_settings = db.query(SettingsModel).all()
        settings_dict = {setting.key: setting.value for setting in db_settings}
        
        config_data = {
            "version": "1.0",
            "exported_at": datetime.now().isoformat(),
            "settings": settings_dict
        }
        
        with open(backup_dir / "config.json", "w") as f:
            json.dump(config_data, f, indent=2)
        
        # Create backup info
        info = {
            "backup_id": backup_id,
            "created_at": datetime.now().isoformat(),
            "system_version": "1.0.0"
        }
        
        with open(backup_dir / "backup_info.json", "w") as f:
            json.dump(info, f, indent=2)
        
        # Create zip file
        zip_path = Path("data/backups") / f"backup_{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in backup_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(backup_dir))
        
        # Clean up temporary directory
        shutil.rmtree(backup_dir)
        
        return {
            "backup_id": backup_id,
            "file_path": str(zip_path),
            "message": "Backup created successfully"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/backup/{backup_id}")
def download_backup(
    backup_id: str,
    current_user: User = Depends(require_admin_flexible),
):
    """Download a backup file (admin only).

    The backup archive contains every configuration setting (including
    OpenAI/Azure keys and the JWT secret), so this must require admin
    authentication. The backup_id is also validated to prevent path
    traversal: the resolved path must stay inside data/backups, and the
    id is constrained to a safe character set.
    """
    import re

    # Restrict backup_id to a safe character set; the timestamp generator
    # above only emits digits and underscores, so this is sufficient.
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", backup_id):
        raise HTTPException(status_code=400, detail="Invalid backup id")

    backups_base = Path("data/backups").resolve()
    zip_path = (backups_base / f"backup_{backup_id}.zip").resolve()

    # Ensure the resolved path stays inside the backups directory.
    try:
        zip_path.relative_to(backups_base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup id")

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=f"docmanager_backup_{backup_id}.zip"
    )

@router.get("/logs/download")
def download_logs(
    current_user: User = Depends(require_admin_flexible),
):
    """Download all log files as a zip archive (admin only).

    Logs typically include request paths, user identifiers, IP addresses
    and error stack traces — all sensitive operational data.
    """
    try:
        # Create a temporary directory for the zip file
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Create timestamp for filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_filename = f"logs_{timestamp}.zip"
            zip_path = temp_path / zip_filename
            
            # Get logs directory
            logs_dir = Path("data/logs")
            if not logs_dir.exists():
                raise HTTPException(status_code=404, detail="Logs directory not found")
            
            # Create zip file with all logs
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add all log files
                for log_file in logs_dir.glob("*.log"):
                    zf.write(log_file, f"data/logs/{log_file.name}")
                
                # Add rotated/compressed logs
                for log_file in logs_dir.glob("*.log.*"):
                    zf.write(log_file, f"data/logs/{log_file.name}")
                
                # Add system info
                system_info = {
                    "exported_at": datetime.now().isoformat(),
                    "logs_directory": str(logs_dir.absolute()),
                    "log_files": [f.name for f in logs_dir.glob("*.log*")]
                }
                
                info_path = temp_path / "logs_info.json"
                with open(info_path, "w") as f:
                    json.dump(system_info, f, indent=2)
                
                zf.write(info_path, "logs_info.json")
            
            # Create a persistent copy before returning
            output_dir = Path("data/temp_downloads")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / zip_filename
            shutil.copy2(zip_path, output_path)
            
            # Schedule cleanup after response
            def cleanup():
                try:
                    if output_path.exists():
                        output_path.unlink()
                except Exception:
                    pass
            
            import threading
            timer = threading.Timer(60.0, cleanup)  # Clean up after 60 seconds
            timer.start()
            
            return FileResponse(
                path=output_path,
                media_type="application/zip",
                filename=f"document_manager_logs_{timestamp}.zip",
                headers={
                    "Content-Disposition": f"attachment; filename=document_manager_logs_{timestamp}.zip"
                }
            )
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create logs archive: {str(e)}")

@router.post("/initialize-defaults")
def initialize_default_settings_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_flexible)
):
    """Initialize missing default settings in the database"""
    try:
        created_settings = initialize_default_settings(db)
        
        if created_settings:
            return {
                "message": f"Successfully initialized {len(created_settings)} default settings",
                "created_settings": list(created_settings.keys())
            }
        else:
            return {
                "message": "All default settings already exist in database",
                "created_settings": []
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize default settings: {str(e)}")