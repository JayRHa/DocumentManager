from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
import os
from pathlib import Path
import mimetypes

from ..database import get_db
from ..models import Document, ProcessingLog, Tag, User
from ..schemas import Document as DocumentSchema, DocumentUpdate, DocumentProcessingStatus, FileUploadResponse, StagingFile, DocumentApprovalRequest, DocumentApprovalResponse
from ..services.document_processor import DocumentProcessor
from ..services.folder_setup import get_folder_info
from ..services.auth_service import get_current_user_flexible, require_permission_flexible
from ..config import get_settings
from ..utils.file_security import (
    validate_file_upload, secure_file_path, set_secure_permissions,
    check_file_permissions, check_document_access, FileSecurityError,
    FileTypeNotAllowedError
)
from datetime import datetime

router = APIRouter()

# Note: Services will be initialized with database session in each endpoint

@router.get("/filter-options")
def get_filter_options(current_user: User = Depends(require_permission_flexible("documents.read"))):
    """Get available filter options for the frontend"""
    date_ranges = [
        {"key": "today", "label": "Heute"},
        {"key": "yesterday", "label": "Gestern"},
        {"key": "last_7_days", "label": "Letzte 7 Tage"},
        {"key": "last_30_days", "label": "Letzte 30 Tage"},
        {"key": "last_90_days", "label": "Letzte 90 Tage"},
        {"key": "this_week", "label": "Diese Woche"},
        {"key": "last_week", "label": "Letzte Woche"},
        {"key": "this_month", "label": "Dieser Monat"},
        {"key": "last_month", "label": "Letzter Monat"},
        {"key": "this_quarter", "label": "Dieses Quartal"},
        {"key": "last_quarter", "label": "Letztes Quartal"},
        {"key": "this_year", "label": "Dieses Jahr"},
        {"key": "last_year", "label": "Letztes Jahr"},
        {"key": "last_2_years", "label": "Letzte 2 Jahre"}
    ]
    
    reminder_options = [
        {"key": "has", "label": "Mit Erinnerung"},
        {"key": "overdue", "label": "Überfällig"},
        {"key": "none", "label": "Ohne Erinnerung"}
    ]
    
    # Sort all options alphabetically by German label
    date_ranges_sorted = sorted(date_ranges, key=lambda x: x["label"])
    reminder_options_sorted = sorted(reminder_options, key=lambda x: x["label"])
    
    return {
        "date_ranges": date_ranges_sorted,
        "reminder_options": reminder_options_sorted
    }

@router.get("/", response_model=List[DocumentSchema])
def get_documents(
    current_user: User = Depends(require_permission_flexible("documents.read")),
    skip: int = 0,
    limit: int = 20,
    correspondent_id: Optional[str] = None,
    doctype_id: Optional[str] = None,
    is_tax_relevant: Optional[bool] = None,
    date_range: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    reminder_filter: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get all documents with optional filtering"""
    query = db.query(Document)
    
    if correspondent_id:
        query = query.filter(Document.correspondent_id == correspondent_id)
    
    if doctype_id:
        query = query.filter(Document.doctype_id == doctype_id)
    
    if is_tax_relevant is not None:
        query = query.filter(Document.is_tax_relevant == is_tax_relevant)
    
    # Date range filtering - predefined ranges take precedence
    if date_range:
        from ..services.search_service import SearchService
        search_service = SearchService()
        start_date, end_date = search_service._calculate_date_range(date_range)
        if start_date and end_date:
            query = query.filter(Document.document_date >= start_date, Document.document_date <= end_date)
    else:
        # Fallback to custom date range
        if date_from:
            try:
                from datetime import datetime
                date_from_parsed = datetime.fromisoformat(date_from)
                query = query.filter(Document.document_date >= date_from_parsed)
            except ValueError:
                pass  # Invalid date format, ignore filter
        
        if date_to:
            try:
                from datetime import datetime
                date_to_parsed = datetime.fromisoformat(date_to)
                query = query.filter(Document.document_date <= date_to_parsed)
            except ValueError:
                pass  # Invalid date format, ignore filter
    
    # Reminder filtering
    if reminder_filter == "has":
        query = query.filter(Document.reminder_date.isnot(None))
    elif reminder_filter == "overdue":
        from datetime import datetime
        query = query.filter(
            Document.reminder_date.isnot(None),
            Document.reminder_date < datetime.utcnow()
        )
    
    documents = query.offset(skip).limit(limit).all()
    return documents

@router.get("/{document_id}", response_model=DocumentSchema)
def get_document(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get a specific document by ID"""
    from sqlalchemy.orm import joinedload
    
    document = db.query(Document)\
        .options(
            joinedload(Document.correspondent),
            joinedload(Document.doctype),
            joinedload(Document.tags)
        )\
        .filter(Document.id == document_id)\
        .first()
    
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document

@router.post("/{document_id}/view")
def track_document_view(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Track document view - increment view count"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Increment view count and update last viewed timestamp
    document.view_count = (document.view_count or 0) + 1
    document.last_viewed = datetime.utcnow()
    
    db.commit()
    
    return {
        "view_count": document.view_count,
        "last_viewed": document.last_viewed
    }

@router.put("/{document_id}", response_model=DocumentSchema)
def update_document(
    document_id: str,
    document_update: DocumentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Update document metadata"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Update fields
    update_data = document_update.dict(exclude_unset=True)
    
    # Handle tags separately
    if 'tag_ids' in update_data:
        tag_ids = update_data.pop('tag_ids')
        if tag_ids is not None:
            # Clear existing tags and add new ones
            document.tags.clear()
            if tag_ids:
                from ..models import Tag
                tags = db.query(Tag).filter(Tag.id.in_(tag_ids)).all()
                document.tags.extend(tags)
    
    # Update other fields
    for field, value in update_data.items():
        setattr(document, field, value)
    
    db.commit()
    db.refresh(document)
    return document

@router.delete("/{document_id}")
def delete_document(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.delete"))
):
    """Delete a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Delete physical file
    try:
        file_path = Path(document.file_path)
        if file_path.exists():
            file_path.unlink()
            print(f"Deleted physical file: {file_path}")
        else:
            print(f"Physical file not found: {file_path}")
    except Exception as e:
        print(f"Error deleting physical file: {e}")
        # Log error but don't fail the deletion
        pass
    
    # Delete from vector database
    try:
        from ..services.vector_db_service import VectorDBService
        vector_db = VectorDBService(db)
        vector_db.delete_document(document_id)
        print(f"Deleted from vector database: {document_id}")
    except Exception as e:
        print(f"Error deleting from vector database: {e}")
        # Log error but don't fail the deletion
        pass
    
    # Delete related processing logs
    try:
        processing_logs = db.query(ProcessingLog).filter(ProcessingLog.document_id == document_id).all()
        for log in processing_logs:
            db.delete(log)
        print(f"Deleted {len(processing_logs)} processing log entries")
    except Exception as e:
        print(f"Error deleting processing logs: {e}")
    
    # Delete document-tag associations
    try:
        document.tags.clear()
        print("Cleared tag associations")
    except Exception as e:
        print(f"Error clearing tag associations: {e}")
    
    # Delete from database
    db.delete(document)
    db.commit()
    
    print(f"Successfully deleted document: {document_id}")
    return {"message": "Document deleted successfully"}

@router.get("/{document_id}/download")
def download_document(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Download the original document file with security checks"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check document access permissions
    if not check_document_access(document, current_user, 'read'):
        raise HTTPException(status_code=403, detail="Access denied to this document")
    
    file_path = Path(document.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found")
    
    # Verify file permissions
    if not check_file_permissions(file_path, current_user):
        raise HTTPException(status_code=403, detail="Access denied to file")
    
    # Validate file path to prevent directory traversal
    try:
        settings = get_settings(db)
        storage_base = Path(settings.storage_folder)
        file_path.relative_to(storage_base)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid file path")
    
    # Log access event
    from ..services.audit_service import log_audit_event
    log_audit_event(
        db=db,
        user_id=current_user.id,
        action="document.download",
        resource_type="document",
        resource_id=document_id,
        details={
            "filename": document.original_filename,
            "file_path": str(file_path)
        }
    )
    
    # Determine media type
    media_type, _ = mimetypes.guess_type(str(file_path))
    if not media_type:
        media_type = "application/octet-stream"
    
    return FileResponse(
        path=str(file_path),
        filename=document.original_filename,
        media_type=media_type
    )

@router.post("/upload", response_model=FileUploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_flexible)
):
    """Upload a new document to staging folder with security validation"""
    
    # Check if user has permission (admin or documents.create)
    if not current_user.is_admin and not current_user.has_permission("documents.create"):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to upload documents"
        )
    
    # Get settings with database session
    settings = get_settings(db)
    
    # Read file content
    content = await file.read()
    
    try:
        # Validate file upload with security checks
        safe_filename, mime_type = validate_file_upload(
            filename=file.filename,
            content=content,
            user=current_user,
            max_size=settings.max_file_size_bytes
        )
        
        # Create secure file path
        staging_base = Path(settings.staging_folder)
        
        # Ensure staging directory exists
        try:
            staging_base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise Exception(f"Failed to create staging directory {staging_base}: {str(e)}")
        
        # Check if staging directory is writable
        if not os.access(staging_base, os.W_OK):
            raise Exception(f"Staging directory {staging_base} is not writable")
            
        staging_path = secure_file_path(staging_base, safe_filename)
        
        # Handle duplicate filenames
        counter = 1
        original_stem = staging_path.stem
        while staging_path.exists():
            new_filename = f"{original_stem}_{counter}{staging_path.suffix}"
            staging_path = secure_file_path(staging_base, new_filename)
            counter += 1
        
        # Save file with secure permissions
        with open(staging_path, "wb") as buffer:
            buffer.write(content)

        # Set secure file permissions. Uploaded documents frequently contain
        # sensitive personal data (invoices, contracts, tax documents, etc.).
        # We default to owner-only (0600) instead of world-readable (0644) so
        # that other local users on the host can't read uploaded documents
        # by browsing the filesystem.
        set_secure_permissions(staging_path, is_private=True)
        
        # Log upload event
        from ..services.audit_service import log_audit_event
        log_audit_event(
            db=db,
            user_id=current_user.id,
            action="document.upload",
            resource_type="document",
            resource_id=None,
            details={
                "filename": safe_filename,
                "original_filename": file.filename,
                "mime_type": mime_type,
                "size": len(content),
                "staging_path": str(staging_path)
            }
        )
        
        # The file watcher will automatically pick up and process this file
        return FileUploadResponse(
            message=f"File uploaded successfully to staging: {staging_path.name}",
            status="uploaded"
        )
        
    except FileTypeNotAllowedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Log the full error for debugging
        import traceback
        error_details = traceback.format_exc()
        print(f"Upload error: {str(e)}")
        print(f"Full traceback: {error_details}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

@router.get("/staging/files", response_model=List[StagingFile])
def get_staging_files(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get list of files in staging folder"""
    settings = get_settings(db)
    staging_path = Path(settings.staging_folder)
    
    if not staging_path.exists():
        return []
    
    files = []
    for file_path in staging_path.iterdir():
        if file_path.is_file():
            stat = file_path.stat()
            files.append(StagingFile(
                filename=file_path.name,
                size=stat.st_size,
                created_at=stat.st_ctime,
                status="pending"  # This would need to be tracked in a separate system
            ))
    
    return files

@router.post("/process-staging")
async def process_staging_files(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.create"))
):
    """Manually trigger processing of all files in staging"""
    from ..services.file_watcher import FileWatcher
    
    file_watcher = FileWatcher()
    background_tasks.add_task(file_watcher.scan_and_process)
    
    return {"message": "Started processing staging files"}

@router.get("/{document_id}/status", response_model=DocumentProcessingStatus)
def get_processing_status(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get processing status for a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return DocumentProcessingStatus(
        document_id=document.id,
        filename=document.filename,
        ocr_status=document.ocr_status,
        ai_status=document.ai_status
    )

@router.get("/{document_id}/logs")
def get_processing_logs(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get processing logs for a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    logs = (db.query(ProcessingLog)
           .filter(ProcessingLog.document_id == document_id)
           .order_by(ProcessingLog.created_at.desc())
           .all())
    
    return logs


@router.post("/{document_id}/reprocess")
async def reprocess_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Reprocess a document (OCR and AI extraction)"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    file_path = Path(document.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found")
    
    # Reset processing status
    document.ocr_status = "pending"
    document.ai_status = "pending"
    document.full_text = None
    document.processed_at = None
    db.commit()
    
    # Reprocess in background - use reprocess_existing method instead of process_file
    document_processor = DocumentProcessor(db)
    background_tasks.add_task(document_processor.reprocess_existing, document, db)
    
    return {"message": "Document reprocessing started"}

@router.get("/stats/overview")
def get_document_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get overview statistics"""
    total_documents = db.query(Document).count()
    pending_ocr = db.query(Document).filter(Document.ocr_status == "pending").count()
    pending_ai = db.query(Document).filter(Document.ai_status == "pending").count()
    tax_relevant = db.query(Document).filter(Document.is_tax_relevant).count()
    
    folder_info = get_folder_info(db)
    
    return {
        "total_documents": total_documents,
        "pending_ocr": pending_ocr,
        "pending_ai": pending_ai,
        "tax_relevant_documents": tax_relevant,
        "folders": folder_info
    }

@router.get("/{document_id}/file")
async def get_document_file(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Serve the actual document file for viewing"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    file_path = Path(document.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found")
    
    # Determine the media type
    media_type, _ = mimetypes.guess_type(str(file_path))
    if media_type is None:
        media_type = "application/octet-stream"
    
    # For PDFs and images, serve inline; for other files, include filename for download
    if media_type in ["application/pdf", "image/jpeg", "image/png", "image/gif", "image/bmp", "image/tiff"]:
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff"
            }
        )
    else:
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=document.original_filename
        )

@router.get("/{document_id}/thumbnail")
def get_document_thumbnail(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get a thumbnail/preview of the document (for images/PDFs)"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    file_path = Path(document.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found")
    
    # For now, just return the file itself
    # In production, you might want to generate actual thumbnails
    media_type, _ = mimetypes.guess_type(str(file_path))
    if media_type is None:
        media_type = "application/octet-stream"
    
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers={"Content-Disposition": "inline"}
    )

@router.post("/{document_id}/tags/{tag_id}")
def add_tag_to_document(
    document_id: str,
    tag_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Add a tag to a document"""
    from ..models import DocumentTag, Tag
    
    # Check if document exists
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check if tag exists
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    
    # Check if relationship already exists
    existing = db.query(DocumentTag).filter(
        DocumentTag.document_id == document_id,
        DocumentTag.tag_id == tag_id
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Tag already associated with document")
    
    # Create association
    doc_tag = DocumentTag(document_id=document_id, tag_id=tag_id)
    db.add(doc_tag)
    db.commit()
    
    return {"message": "Tag added to document"}

@router.delete("/{document_id}/tags/{tag_id}")
def remove_tag_from_document(
    document_id: str,
    tag_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Remove a tag from a document"""
    # Check if document exists
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check if tag exists
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    
    # Check if tag is associated with document
    if tag not in document.tags:
        raise HTTPException(status_code=404, detail="Tag not associated with document")
    
    # Remove tag from document using SQLAlchemy relationship
    document.tags.remove(tag)
    db.commit()
    
    return {"message": "Tag removed from document"}

@router.post("/{document_id}/tags")
def create_and_add_tag_to_document(
    document_id: str,
    tag_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Create a new tag and add it to a document"""
    from ..models import Tag, DocumentTag
    import uuid
    
    tag_name = tag_data.get("tag_name", "").strip()
    if not tag_name:
        raise HTTPException(status_code=400, detail="Tag name is required")
    
    # Check if document exists
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check if tag already exists
    existing_tag = db.query(Tag).filter(Tag.name == tag_name).first()
    if existing_tag:
        # Tag exists, check if already associated with document
        existing_assoc = db.query(DocumentTag).filter(
            DocumentTag.document_id == document_id,
            DocumentTag.tag_id == existing_tag.id
        ).first()
        
        if existing_assoc:
            raise HTTPException(status_code=400, detail="Tag already associated with document")
        
        # Associate existing tag with document
        doc_tag = DocumentTag(document_id=document_id, tag_id=existing_tag.id)
        db.add(doc_tag)
        db.commit()
        return {"message": "Existing tag added to document", "tag_id": existing_tag.id}
    
    # Create new tag
    new_tag = Tag(
        id=str(uuid.uuid4()),
        name=tag_name
    )
    db.add(new_tag)
    db.flush()  # Get the ID
    
    # Associate with document
    doc_tag = DocumentTag(document_id=document_id, tag_id=new_tag.id)
    db.add(doc_tag)
    db.commit()
    
    return {"message": "New tag created and added to document", "tag_id": new_tag.id}

@router.post("/cleanup/orphaned")
def cleanup_orphaned_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.delete"))
):
    """Remove orphaned document entries where the physical file no longer exists"""
    documents = db.query(Document).all()
    orphaned_count = 0
    cleaned_documents = []
    
    for document in documents:
        if document.file_path:
            file_path = Path(document.file_path)
            if not file_path.exists():
                # This is an orphaned document
                orphaned_count += 1
                
                # Store info before deletion
                cleaned_documents.append({
                    "id": document.id,
                    "filename": document.original_filename,
                    "file_path": document.file_path,
                    "hash": document.file_hash
                })
                
                # Delete from vector database
                try:
                    from ..services.vector_db_service import VectorDBService
                    vector_db = VectorDBService()
                    vector_db.delete_document(document.id)
                except Exception as e:
                    print(f"Error deleting from vector database: {e}")
                
                # Delete related processing logs
                try:
                    processing_logs = db.query(ProcessingLog).filter(ProcessingLog.document_id == document.id).all()
                    for log in processing_logs:
                        db.delete(log)
                except Exception as e:
                    print(f"Error deleting processing logs: {e}")
                
                # Clear tag associations
                try:
                    document.tags.clear()
                except Exception as e:
                    print(f"Error clearing tag associations: {e}")
                
                # Delete the document
                db.delete(document)
    
    db.commit()
    
    return {
        "message": f"Cleaned up {orphaned_count} orphaned document entries",
        "cleaned_documents": cleaned_documents,
        "count": orphaned_count
    }

@router.post("/{document_id}/reprocess-ai")
async def reprocess_ai_only(
    document_id: str, 
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Retry only AI processing for a document with failed AI but successful OCR"""
    from ..services.document_processor import DocumentProcessor
    from ..models import ProcessingLog
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    if document.ocr_status != "completed":
        raise HTTPException(status_code=400, detail="OCR must be completed before retrying AI processing")
    
    try:
        # Reset AI status only
        document.ai_status = "pending"
        document.summary = None
        document.processed_at = None
        
        # Log retry attempt
        log = ProcessingLog(
            document_id=document_id,
            operation="ai_retry",
            status="info",
            message="AI processing queued for retry"
        )
        db.add(log)
        db.commit()
        
        # Get file path for reprocessing
        file_path = Path(document.file_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Document file not found")
        
        # Trigger reprocessing in background
        # The DocumentProcessor will check the status and only do AI processing
        document_processor = DocumentProcessor(db)
        background_tasks.add_task(document_processor.process_file, file_path, db)
        
        return {"message": "AI processing queued for retry"}
        
    except Exception as e:
        # Log failure
        log = ProcessingLog(
            document_id=document_id,
            operation="ai_retry",
            status="error",
            message=f"Failed to queue AI retry: {str(e)}"
        )
        db.add(log)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to retry AI processing: {str(e)}")

@router.post("/{document_id}/reprocess-ocr")
def reprocess_ocr_only(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Retry only OCR processing for a document"""
    from ..services.document_processor import DocumentProcessor
    from ..models import ProcessingLog
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    try:
        # Reset OCR status only
        document.ocr_status = "pending"
        document.full_text = None
        
        # Log retry attempt
        log = ProcessingLog(
            document_id=document_id,
            operation="ocr_retry",
            status="info",
            message="OCR processing queued for retry"
        )
        db.add(log)
        db.commit()
        
        # Trigger OCR processing only
        processor = DocumentProcessor()
        processor._process_ocr(document, db)
        
        return {"message": "OCR processing queued for retry"}
        
    except Exception as e:
        # Log failure
        log = ProcessingLog(
            document_id=document_id,
            operation="ocr_retry",
            status="error",
            message=f"Failed to queue OCR retry: {str(e)}"
        )
        db.add(log)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to retry OCR processing: {str(e)}")

@router.post("/{document_id}/reprocess-vector")
def reprocess_vector_only(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Retry only vectorization for a document"""
    from ..services.document_processor import DocumentProcessor
    from ..models import ProcessingLog
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    if not document.full_text:
        raise HTTPException(status_code=400, detail="Document must have OCR text before vectorization")
    
    try:
        # Reset vector status only
        document.vector_status = "pending"
        
        # Log retry attempt
        log = ProcessingLog(
            document_id=document_id,
            operation="vector_retry",
            status="info",
            message="Vectorization processing queued for retry"
        )
        db.add(log)
        db.commit()
        
        # Trigger vectorization only
        processor = DocumentProcessor()
        processor._create_embeddings(document, db)
        
        return {"message": "Vectorization processing queued for retry"}
        
    except Exception as e:
        # Log failure
        log = ProcessingLog(
            document_id=document_id,
            operation="vector_retry",
            status="error",
            message=f"Failed to queue vectorization retry: {str(e)}"
        )
        db.add(log)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to retry vectorization: {str(e)}")

# Document Relations endpoints
@router.get("/{document_id}/relations")
def get_document_relations(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get all related documents (both parents and children)"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Get both parent and child relations
    return {
        "document_id": document_id,
        "parent_documents": [
            {
                "id": parent.id,
                "title": parent.title or parent.filename,
                "filename": parent.filename,
                "document_date": parent.document_date,
                "correspondent": parent.correspondent.name if parent.correspondent else None
            }
            for parent in document.parents
        ],
        "child_documents": [
            {
                "id": child.id,
                "title": child.title or child.filename,
                "filename": child.filename,
                "document_date": child.document_date,
                "correspondent": child.correspondent.name if child.correspondent else None
            }
            for child in document.children
        ]
    }

@router.post("/{document_id}/relations/{related_document_id}")
def add_document_relation(
    document_id: str,
    related_document_id: str,
    relation_type: str = "child",  # "child" or "parent"
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Add a relation between two documents"""
    if document_id == related_document_id:
        raise HTTPException(status_code=400, detail="Cannot relate a document to itself")
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    related_document = db.query(Document).filter(Document.id == related_document_id).first()
    if not related_document:
        raise HTTPException(status_code=404, detail="Related document not found")
    
    # Add relation based on type
    if relation_type == "child":
        if related_document not in document.children:
            document.children.append(related_document)
    else:  # parent
        if document not in related_document.children:
            related_document.children.append(document)
    
    db.commit()
    
    return {"message": "Relation added successfully"}

@router.delete("/{document_id}/relations/{related_document_id}")
def remove_document_relation(
    document_id: str,
    related_document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Remove a relation between two documents"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    related_document = db.query(Document).filter(Document.id == related_document_id).first()
    if not related_document:
        raise HTTPException(status_code=404, detail="Related document not found")
    
    # Remove from both directions
    if related_document in document.children:
        document.children.remove(related_document)
    if document in related_document.children:
        related_document.children.remove(document)
    
    db.commit()
    
    return {"message": "Relation removed successfully"}

@router.get("/{document_id}/similar")
def find_similar_documents(
    document_id: str,
    limit: int = 10,
    threshold: float = 0.3,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Find similar documents using vector similarity search"""
    from ..services.vector_db_service import VectorDBService
    from ..services.ai_service import AIService
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Check if document has embeddings - if not, try to proceed anyway
    if document.vector_status != "completed":
        print(f"Warning: Document {document_id} has vector_status: {document.vector_status}")
        # Don't fail, just proceed with search - the vector DB might still have embeddings
    
    try:
        # Initialize services with better error handling
        try:
            vector_db = VectorDBService(db)
        except Exception as e:
            print(f"Error initializing VectorDBService: {e}")
            raise HTTPException(status_code=500, detail="Vector database service not available")
        
        try:
            ai_service = AIService(db_session=db)
        except Exception as e:
            print(f"Error initializing AIService: {e}")
            raise HTTPException(status_code=500, detail="AI service not configured or not available")
        
        # Create search text from document
        search_text = f"{document.title or ''} {document.summary or ''}"
        if not search_text.strip() and document.full_text:
            search_text = document.full_text[:1000]
        
        if not search_text.strip():
            print(f"No search text available for document {document_id}")
            return {
                "document_id": document_id,
                "similar_documents": [],
                "count": 0,
                "message": "No text content available for similarity search"
            }
        
        # Generate embeddings for search
        try:
            search_embeddings = ai_service.generate_embeddings(search_text)
        except Exception as e:
            print(f"Error generating embeddings: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate embeddings for similarity search")
        
        # Search for similar documents
        try:
            results = vector_db.search_similar(
                query_embeddings=search_embeddings,
                limit=limit + 1  # +1 because the document itself might be included
            )
        except Exception as e:
            print(f"Error searching similar documents: {e}")
            raise HTTPException(status_code=500, detail="Failed to search vector database")
        
        # Filter out the document itself and apply threshold
        similar_docs = []
        for result in results:
            if result['id'] != document_id and result.get('score', 0) >= threshold:
                # Get document details from database
                sim_doc = db.query(Document).filter(Document.id == result['id']).first()
                if sim_doc:
                    similar_docs.append({
                        "id": sim_doc.id,
                        "title": sim_doc.title or sim_doc.filename,
                        "filename": sim_doc.filename,
                        "document_date": sim_doc.document_date,
                        "correspondent": sim_doc.correspondent.name if sim_doc.correspondent else None,
                        "similarity_score": result.get('score', 0),
                        "summary": sim_doc.summary
                    })
        
        return {
            "document_id": document_id,
            "similar_documents": similar_docs,
            "count": len(similar_docs)
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions as they are already properly formatted
        raise
    except Exception as e:
        print(f"Unexpected error in find_similar_documents: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to find similar documents: {str(e)}")

# Document Notes endpoints
@router.put("/{document_id}/notes")
def update_document_notes(
    document_id: str,
    notes_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.update"))
):
    """Update notes for a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    notes = notes_data.get("notes", "")
    document.notes = notes
    db.commit()
    db.refresh(document)
    
    return {
        "message": "Notes updated successfully",
        "notes": document.notes
    }

@router.get("/{document_id}/notes")
def get_document_notes(
    document_id: str, 
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get notes for a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return {
        "document_id": document_id,
        "notes": document.notes or ""
    }

@router.post("/{document_id}/approve", response_model=DocumentApprovalResponse)
def approve_document(
    document_id: str,
    approval_request: DocumentApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.approve"))
):
    """Approve or disapprove a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Update approval status
    document.is_approved = approval_request.approved
    document.approved_by = current_user.id if approval_request.approved else None
    document.approved_at = datetime.utcnow() if approval_request.approved else None
    
    db.commit()
    db.refresh(document)
    
    return DocumentApprovalResponse(
        success=True,
        message="Document approved successfully" if approval_request.approved else "Document approval removed",
        document_id=document_id,
        is_approved=document.is_approved,
        approved_at=document.approved_at,
        approved_by=document.approved_by
    )

@router.get("/{document_id}/approval-status")
def get_document_approval_status(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission_flexible("documents.read"))
):
    """Get approval status of a document"""
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    approved_by_user = None
    if document.approved_by:
        approved_by_user = db.query(User).filter(User.id == document.approved_by).first()
    
    return {
        "document_id": document_id,
        "is_approved": document.is_approved,
        "approved_at": document.approved_at,
        "approved_by": document.approved_by,
        "approved_by_user": {
            "id": approved_by_user.id,
            "username": approved_by_user.username,
            "full_name": approved_by_user.full_name
        } if approved_by_user else None
    }
