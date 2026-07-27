import shutil
import magic
from pathlib import Path
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from loguru import logger

from ..models import Document, Correspondent, DocType, Tag, ProcessingLog
from ..schemas import AIExtractedData
from .file_utils import calculate_file_hash, get_file_info
from .ocr_service import OCRService
from .ai_service import AIService
from .vector_db_service import VectorDBService
from ..config import get_settings

class DocumentProcessor:
    """Main document processing pipeline"""
    
    def __init__(self, db: Session = None):
        # Create a database session if none provided for settings loading
        if db is None:
            from ..database import SessionLocal
            with SessionLocal() as session:
                self.settings = get_settings(session)
        else:
            self.settings = get_settings(db)
            
        self.ocr_service = OCRService(db)
        try:
            # Use a fresh session for AI service initialization
            if db is None:
                from ..database import SessionLocal
                with SessionLocal() as session:
                    self.ai_service = AIService(db_session=session)
            else:
                self.ai_service = AIService(db_session=db)
        except ValueError as e:
            if "API key" in str(e):
                logger.warning("AI service not available - API key not configured")
                self.ai_service = None
            else:
                raise
        self.vector_db = VectorDBService(db)
    
    def process_file(self, file_path: Path, db: Session) -> Optional[Document]:
        """Main processing pipeline for a single file"""
        start_time = datetime.now()
        
        # Initialize AI service with database session for dynamic types
        if self.ai_service is None:
            try:
                self.ai_service = AIService(db_session=db)
            except ValueError as e:
                if "API key" in str(e):
                    logger.warning("AI service not available - API key not configured")
                    self.ai_service = None
                else:
                    raise
        
        try:
            logger.info(f"Starting processing of file: {file_path.name}")
            
            # Step 0: Final check that file still exists
            if not file_path.exists():
                logger.warning(f"File no longer exists at start of processing: {file_path}")
                return None
            
            # Step 1: Validate file
            if not self._validate_file(file_path):
                return None
            
            # Step 2: Calculate hash and check for duplicates
            file_hash = calculate_file_hash(file_path)
            existing_doc = db.query(Document).filter(Document.file_hash == file_hash).first()
            
            if existing_doc:
                # Check if the existing document's file still exists
                existing_file_path = Path(existing_doc.file_path) if existing_doc.file_path else None
                
                if existing_file_path and existing_file_path.exists():
                    # Check if the existing document has failed processing
                    has_failed_processing = (
                        existing_doc.ocr_status == "failed" or 
                        existing_doc.ai_status == "failed" or 
                        existing_doc.vector_status == "failed"
                    )
                    
                    if has_failed_processing:
                        # Document exists but failed processing - allow reprocessing
                        logger.info(f"Found failed document with same hash, removing to allow reprocessing: {existing_doc.id}")
                        self._log_processing(db, existing_doc.id, "retry_check", "info", 
                                           f"Removing failed document (OCR: {existing_doc.ocr_status}, AI: {existing_doc.ai_status}, Vector: {existing_doc.vector_status}) to allow reprocessing")
                        
                        # Remove from vector database if exists
                        try:
                            self.vector_db.delete_document(existing_doc.id)
                        except Exception as e:
                            logger.warning(f"Failed to remove document from vector DB: {e}")
                        
                        # Delete the failed document record
                        db.delete(existing_doc)
                        db.commit()
                        
                        logger.info(f"Proceeding with reprocessing of file: {file_path.name}")
                    else:
                        # File exists and was successfully processed, this is a true duplicate
                        logger.info(f"File already exists (duplicate hash): {file_path.name}")
                        self._log_processing(db, existing_doc.id, "duplicate_check", "info", 
                                           "File already processed (duplicate hash)")
                        # Move file to processed folder or delete
                        self._handle_duplicate_file(file_path)
                        return existing_doc
                else:
                    # File doesn't exist anymore, remove the orphaned database entry
                    logger.info(f"Found orphaned document entry (file missing), removing: {existing_doc.id}")
                    self._log_processing(db, existing_doc.id, "cleanup", "info", 
                                       "Removing orphaned document entry (file missing)")
                    
                    # Remove from vector database if exists
                    try:
                        self.vector_db.delete_document(existing_doc.id)
                    except Exception as e:
                        logger.warning(f"Failed to remove document from vector DB: {e}")
                    
                    # Delete the orphaned document record
                    db.delete(existing_doc)
                    db.commit()
                    
                    logger.info(f"Proceeding with processing of new file: {file_path.name}")
            
            # Step 3: Get file info
            file_info = get_file_info(file_path)
            mime_type = self._get_mime_type(file_path)
            
            # Step 4: Create document record (without final file path)
            document = self._create_document_record(file_path, file_info, file_hash, mime_type, db)
            
            # Step 5: Extract text using OCR (while file is still in staging)
            try:
                document.ocr_status = "processing"
                db.commit()
                
                full_text = self.ocr_service.extract_text(file_path)
                document.full_text = full_text
                document.ocr_status = "completed"
                
                self._log_processing(db, document.id, "ocr", "success", 
                                   f"Extracted {len(full_text)} characters")
                
            except Exception as e:
                document.ocr_status = "failed"
                self._log_processing(db, document.id, "ocr", "error", str(e))
                logger.error(f"OCR failed for {file_path.name}: {e}")
            
            db.commit()
            
            # Step 6: Extract metadata using AI
            if document.full_text and self.ai_service:
                try:
                    document.ai_status = "processing"
                    db.commit()
                    
                    extracted_data = self.ai_service.extract_document_metadata(
                        document.full_text, file_path.name
                    )
                    
                    # Apply extracted metadata (this will populate correspondent and document_date)
                    self._apply_extracted_metadata(document, extracted_data, db)
                    
                    document.ai_status = "completed"
                    document.processed_at = datetime.now()
                    
                    self._log_processing(db, document.id, "ai_extraction", "success", 
                                       "Metadata extracted successfully")
                    
                except Exception as e:
                    document.ai_status = "failed"
                    self._log_processing(db, document.id, "ai_extraction", "error", str(e))
                    logger.error(f"AI extraction failed for {file_path.name}: {e}")
            else:
                if not self.ai_service:
                    document.ai_status = "skipped"
                    self._log_processing(db, document.id, "ai_extraction", "info", 
                                       "AI service not available - metadata extraction skipped")
                    logger.info(f"AI service not available - skipping metadata extraction for {file_path.name}")
            
            # Step 7: Now move file to storage with proper structure (we have metadata now)
            # Refresh document to get related data
            db.refresh(document)
            storage_path = self._move_to_storage(file_path, document.id, document)
            document.file_path = str(storage_path)
            db.commit()
            
            # Step 8: Generate and store embeddings
            if document.full_text:
                try:
                    self._store_embeddings(document, db)
                    self._log_processing(db, document.id, "embeddings", "success", 
                                       "Embeddings generated and stored")
                except Exception as e:
                    self._log_processing(db, document.id, "embeddings", "error", str(e))
                    logger.error(f"Embedding generation failed for {file_path.name}: {e}")
            
            # Calculate total processing time
            processing_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"Successfully processed {file_path.name} in {processing_time:.2f} seconds")
            
            return document
            
        except Exception as e:
            logger.error(f"Failed to process file {file_path.name}: {e}")
            self._log_processing(db, None, "processing", "error", str(e))
            return None
    
    def _validate_file(self, file_path: Path) -> bool:
        """Validate file size and extension"""
        # Check extension
        if file_path.suffix.lower() not in [f".{ext}" for ext in self.settings.allowed_extensions_list]:
            logger.warning(f"File extension not allowed: {file_path.suffix}")
            return False
        
        # Check file size
        file_size = file_path.stat().st_size
        if file_size > self.settings.max_file_size_bytes:
            logger.warning(f"File too large: {file_size} bytes > {self.settings.max_file_size_bytes}")
            return False
        
        return True
    
    def _get_mime_type(self, file_path: Path) -> str:
        """Get MIME type of file"""
        try:
            mime = magic.Magic(mime=True)
            return mime.from_file(str(file_path))
        except Exception:
            # Fallback based on extension
            ext_to_mime = {
                '.pdf': 'application/pdf',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.tiff': 'image/tiff',
                '.bmp': 'image/bmp',
                '.txt': 'text/plain',
                '.text': 'text/plain',
                '.md': 'text/markdown',
                '.markdown': 'text/markdown',
            }
            return ext_to_mime.get(file_path.suffix.lower(), 'application/octet-stream')
    
    def _create_document_record(self, file_path: Path, file_info: dict, 
                               file_hash: str, mime_type: str, db: Session) -> Document:
        """Create initial document record in database"""
        document = Document(
            filename=file_path.name,
            original_filename=file_path.name,
            file_hash=file_hash,
            file_path="",  # Will be set after moving to storage
            file_size=file_info["size"],
            mime_type=mime_type
        )
        
        db.add(document)
        db.commit()
        db.refresh(document)
        
        return document
    
    def _move_to_storage(self, file_path: Path, document_id: str, document: Document = None) -> Path:
        """Move file from staging to storage using new structure: {correspondent}/{docdate}/{filename}"""
        storage_dir = Path(self.settings.storage_folder)
        
        # For new structure, we need document info to determine path
        if document and document.correspondent and document.document_date:
            # New structure: {correspondent}/{docdate}/{filename}
            correspondent_name = self._sanitize_folder_name(document.correspondent.name)
            date_str = document.document_date.strftime("%Y-%m-%d")
            
            target_dir = storage_dir / correspondent_name / date_str
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # Use document ID prefix with original filename for uniqueness
            storage_filename = f"{document_id}_{file_path.name}"
            storage_path = target_dir / storage_filename
            
        else:
            # Fallback structure if no correspondent/date available
            if document and document.document_date:
                date_str = document.document_date.strftime("%Y-%m-%d")
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")
            
            target_dir = storage_dir / "unknown_correspondent" / date_str
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # Use document ID prefix with original filename for uniqueness
            storage_filename = f"{document_id}_{file_path.name}"
            storage_path = target_dir / storage_filename
        
        # Ensure no conflicts
        counter = 1
        original_storage_path = storage_path
        while storage_path.exists():
            stem = original_storage_path.stem
            suffix = original_storage_path.suffix
            storage_path = original_storage_path.parent / f"{stem}_{counter}{suffix}"
            counter += 1
        
        # Move file
        shutil.move(str(file_path), str(storage_path))
        
        logger.debug(f"Moved file to storage: {storage_path}")
        return storage_path
    
    def _sanitize_folder_name(self, name: str) -> str:
        """Sanitize name for use as folder name"""
        import re
        # Remove/replace invalid characters
        sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
        # Remove leading/trailing dots and spaces
        sanitized = sanitized.strip(' .')
        # Limit length
        if len(sanitized) > 50:
            sanitized = sanitized[:50].rstrip()
        
        return sanitized or "Unknown"
    
    def _handle_duplicate_file(self, file_path: Path):
        """Handle duplicate files"""
        # Move to a 'duplicates' folder or delete
        duplicates_dir = Path(self.settings.staging_folder) / "duplicates"
        duplicates_dir.mkdir(exist_ok=True)
        
        duplicate_path = duplicates_dir / file_path.name
        shutil.move(str(file_path), str(duplicate_path))
        
        logger.info(f"Moved duplicate file to: {duplicate_path}")
    
    def _apply_extracted_metadata(self, document: Document, extracted_data: AIExtractedData, db: Session):
        """Apply AI-extracted metadata to document"""
        # Update basic fields
        document.title = extracted_data.title
        document.summary = extracted_data.summary
        document.is_tax_relevant = extracted_data.is_tax_relevant
        
        # Parse and set document date
        if extracted_data.document_date:
            try:
                document.document_date = datetime.fromisoformat(extracted_data.document_date)
            except ValueError:
                logger.warning(f"Could not parse date: {extracted_data.document_date}")
        
        # Handle correspondent
        if extracted_data.correspondent_name:
            correspondent = self._get_or_create_correspondent(extracted_data.correspondent_name, db)
            document.correspondent_id = correspondent.id
        
        # Handle document type
        if extracted_data.doctype_name:
            doctype = self._get_or_create_doctype(extracted_data.doctype_name, db)
            document.doctype_id = doctype.id
        
        # Handle tags
        if extracted_data.tag_names:
            for tag_name in extracted_data.tag_names:
                tag = self._get_or_create_tag(tag_name, db)
                if tag not in document.tags:
                    document.tags.append(tag)
    
    def _get_or_create_correspondent(self, name: str, db: Session) -> Correspondent:
        """Get or create correspondent by name"""
        correspondent = db.query(Correspondent).filter(Correspondent.name == name).first()
        if not correspondent:
            correspondent = Correspondent(name=name)
            db.add(correspondent)
            db.commit()
            db.refresh(correspondent)
        return correspondent
    
    def _get_or_create_doctype(self, name: str, db: Session) -> DocType:
        """Get or create document type by name"""
        doctype = db.query(DocType).filter(DocType.name == name).first()
        if not doctype:
            doctype = DocType(name=name)
            db.add(doctype)
            db.commit()
            db.refresh(doctype)
        return doctype
    
    def _get_or_create_tag(self, name: str, db: Session) -> Tag:
        """Get or create tag by name"""
        tag = db.query(Tag).filter(Tag.name == name).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.commit()
            db.refresh(tag)
        return tag
    
    def _store_embeddings(self, document: Document, db: Session):
        """Generate and store embeddings for document"""
        if not document.full_text:
            return
        
        # Enhanced embedding content for better semantic search
        embedding_parts = []
        
        # Add document title if available (weighted heavily for search)
        if document.title:
            embedding_parts.append(f"Titel: {document.title}")
            # Repeat title for emphasis in embeddings
            embedding_parts.append(f"Dokument: {document.title}")
        
        # Add filename as fallback identifier
        if document.filename:
            embedding_parts.append(f"Dateiname: {document.filename}")
        
        # Add correspondent information
        if document.correspondent:
            embedding_parts.append(f"Korrespondent: {document.correspondent.name}")
            embedding_parts.append(f"Von/An: {document.correspondent.name}")
        
        # Add document type with synonyms
        if document.doctype:
            doctype_name = document.doctype.name
            embedding_parts.append(f"Dokumenttyp: {doctype_name}")
            
            # Add common synonyms for document types
            doctype_synonyms = {
                'Rechnung': 'Invoice Faktura Abrechnung',
                'Vertrag': 'Contract Kontrakt Vereinbarung',
                'Brief': 'Letter Korrespondenz Schreiben',
                'Angebot': 'Offer Offerte Proposal'
            }
            
            if doctype_name in doctype_synonyms:
                embedding_parts.append(doctype_synonyms[doctype_name])
        
        # Add date information for temporal context
        if document.document_date:
            date_str = document.document_date.strftime("%d.%m.%Y")
            month_name = document.document_date.strftime("%B")
            year = document.document_date.strftime("%Y")
            embedding_parts.append(f"Datum: {date_str} {month_name} {year}")
        
        # Add tags for semantic richness
        if document.tags:
            tag_names = [tag.name for tag in document.tags]
            embedding_parts.append(f"Tags: {', '.join(tag_names)}")
            # Also add tags individually for better matching
            for tag in tag_names:
                embedding_parts.append(f"Thema: {tag}")
        
        # Add tax relevance as semantic signal
        if document.is_tax_relevant:
            embedding_parts.append("Steuerrelevant Tax-relevant Steuern Finanzamt")
        
        # Add main content (summary gets priority over full text)
        if document.summary:
            embedding_parts.append(f"Zusammenfassung: {document.summary}")
            # Also add more of full text for additional context
            if document.full_text:
                # Use more text to capture all important content
                embedding_parts.append(f"Inhalt: {document.full_text[:4000]}")
        else:
            # Use even more of full text if no summary available
            main_content = document.full_text[:8000] if document.full_text else ""
            if main_content:
                embedding_parts.append(f"Inhalt: {main_content}")
        
        # Combine all parts for richer semantic context
        text_for_embedding = "\n".join(embedding_parts)
        
        # Generate embeddings
        if self.ai_service:
            embeddings = self.ai_service.generate_embeddings(text_for_embedding)
        else:
            logger.warning("AI service not available - skipping embedding generation")
            return
        
        # Prepare metadata for vector DB
        metadata = {
            "document_id": document.id,
            "title": document.title or document.filename,
            "is_tax_relevant": document.is_tax_relevant,
            "created_at": document.created_at.isoformat()
        }
        
        # Only add correspondent and doctype if they exist
        if document.correspondent:
            metadata["correspondent"] = document.correspondent.name
        if document.doctype:
            metadata["doctype"] = document.doctype.name
        
        # Store in vector database
        try:
            self.vector_db.add_document(
                document_id=document.id,
                text=text_for_embedding,
                embeddings=embeddings,
                metadata=metadata
            )
            
            # Update vector status to completed
            document.vector_status = "completed"
            db.commit()
            logger.info(f"Document {document.id} successfully vectorized")
            
        except Exception as e:
            logger.error(f"Failed to vectorize document {document.id}: {e}")
            document.vector_status = "failed"
            db.commit()
            raise
    
    def _log_processing(self, db: Session, document_id: Optional[str], 
                       operation: str, status: str, message: str):
        """Log processing operation"""
        try:
            log_entry = ProcessingLog(
                document_id=document_id,
                operation=operation,
                status=status,
                message=message
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to log processing operation: {e}")
    
    def reprocess_existing(self, document: Document, db: Session) -> Document:
        """Reprocess an existing document without duplicate checking"""
        start_time = datetime.now()
        
        try:
            logger.info(f"Starting reprocessing of document: {document.id}")
            
            # Initialize AI service with database session for dynamic types
            if self.ai_service is None:
                try:
                    self.ai_service = AIService(db_session=db)
                except ValueError as e:
                    if "API key" in str(e):
                        logger.warning("AI service not available - API key not configured")
                        self.ai_service = None
                    else:
                        raise
            
            file_path = Path(document.file_path)
            
            # Check if file exists
            if not file_path.exists():
                logger.error(f"File not found for reprocessing: {file_path}")
                self._log_processing(db, document.id, "reprocess", "error", 
                                   f"File not found: {file_path}")
                raise Exception(f"File not found: {file_path}")
            
            # Step 1: Extract text (OCR)
            if document.ocr_status in ["pending", "failed"]:
                try:
                    logger.info(f"Performing OCR on {file_path.name}")
                    self._log_processing(db, document.id, "ocr", "info", "Starting OCR processing")
                    
                    full_text = self.ocr_service.extract_text(file_path)
                    
                    document.full_text = full_text
                    document.ocr_status = "completed"
                    self._log_processing(db, document.id, "ocr", "success", 
                                       f"OCR completed, extracted {len(full_text)} characters")
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"OCR failed for {file_path.name}: {error_msg}")
                    document.ocr_status = "failed"
                    self._log_processing(db, document.id, "ocr", "error", error_msg)
                    db.commit()
                    return document
            
            # Step 2: AI Processing
            if self.ai_service and document.ai_status in ["pending", "failed"]:
                if document.ocr_status == "completed":
                    try:
                        logger.info(f"Performing AI extraction on {file_path.name}")
                        self._log_processing(db, document.id, "ai", "info", "Starting AI extraction")
                        
                        # Use consistent types from the database
                        correspondents = db.query(Correspondent).all()
                        doctypes = db.query(DocType).all()
                        
                        ai_data = self.ai_service.extract_document_metadata(
                            document.full_text,
                            document.filename
                        )
                        
                        self._apply_extracted_metadata(document, ai_data, db)
                        document.ai_status = "completed"
                        self._log_processing(db, document.id, "ai", "success", "AI extraction completed")
                        
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"AI extraction failed for {file_path.name}: {error_msg}")
                        document.ai_status = "failed"
                        self._log_processing(db, document.id, "ai", "error", error_msg)
                else:
                    logger.info("Skipping AI extraction - OCR not completed")
                    self._log_processing(db, document.id, "ai", "info", 
                                       "Skipping AI extraction - OCR not completed")
            
            # Update processing timestamp
            document.processed_at = datetime.now()
            db.commit()
            
            # Step 3: Vectorization
            if document.ocr_status == "completed":
                try:
                    logger.info(f"Vectorizing document {document.id}")
                    self._log_processing(db, document.id, "vector", "info", "Starting vectorization")
                    
                    document.vector_status = "processing"
                    db.commit()
                    
                    self._store_embeddings(document, db)
                    self._log_processing(db, document.id, "vector", "success", "Vectorization completed")
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Vectorization failed for document {document.id}: {error_msg}")
                    document.vector_status = "failed"
                    self._log_processing(db, document.id, "vector", "error", error_msg)
                    db.commit()
            
            # Calculate processing time
            processing_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"Reprocessing completed for {file_path.name} in {processing_time:.1f} seconds")
            self._log_processing(db, document.id, "reprocess", "success", 
                               f"Reprocessing completed in {processing_time:.1f} seconds")
            
            return document
            
        except Exception as e:
            logger.error(f"Failed to reprocess document {document.id}: {e}")
            self._log_processing(db, document.id, "reprocess", "error", str(e))
            raise
    
    def cleanup_orphaned_documents(self, db: Session) -> int:
        """Remove orphaned document entries where the physical file no longer exists"""
        documents = db.query(Document).all()
        orphaned_count = 0
        
        for document in documents:
            if document.file_path:
                file_path = Path(document.file_path)
                if not file_path.exists():
                    orphaned_count += 1
                    
                    logger.info(f"Removing orphaned document: {document.id} - {document.original_filename}")
                    
                    # Remove from vector database
                    try:
                        self.vector_db.delete_document(document.id)
                    except Exception as e:
                        logger.warning(f"Failed to remove document from vector DB: {e}")
                    
                    # Delete related processing logs
                    try:
                        processing_logs = db.query(ProcessingLog).filter(ProcessingLog.document_id == document.id).all()
                        for log in processing_logs:
                            db.delete(log)
                    except Exception as e:
                        logger.warning(f"Failed to delete processing logs: {e}")
                    
                    # Clear tag associations
                    try:
                        document.tags.clear()
                    except Exception as e:
                        logger.warning(f"Failed to clear tag associations: {e}")
                    
                    # Delete the document
                    db.delete(document)
        
        if orphaned_count > 0:
            db.commit()
            logger.info(f"Cleaned up {orphaned_count} orphaned document entries")
        
        return orphaned_count
