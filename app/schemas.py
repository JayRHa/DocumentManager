from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime

# Base schemas
class CorrespondentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    address: Optional[str] = None

class CorrespondentCreate(CorrespondentBase):
    pass

class CorrespondentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    address: Optional[str] = None

class Correspondent(CorrespondentBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

# DocType schemas
class DocTypeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None

class DocTypeCreate(DocTypeBase):
    pass

class DocTypeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None

class DocType(DocTypeBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

# Tag schemas
class TagBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    color: Optional[str] = Field(None, max_length=7)  # hex color

class TagCreate(TagBase):
    pass

class TagUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    color: Optional[str] = Field(None, max_length=7)

class Tag(TagBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

# Extended schemas with document counts
class CorrespondentWithCount(Correspondent):
    document_count: int = 0

class DocTypeWithCount(DocType):
    document_count: int = 0

class TagWithCount(Tag):
    document_count: int = 0

# Document schemas
class DocumentBase(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    document_date: Optional[datetime] = None
    is_tax_relevant: bool = False
    reminder_date: Optional[datetime] = None
    notes: Optional[str] = None
    correspondent_id: Optional[str] = None
    doctype_id: Optional[str] = None

class DocumentCreate(DocumentBase):
    filename: str
    file_hash: str
    file_path: str
    file_size: int
    mime_type: str
    original_filename: str

class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    document_date: Optional[datetime] = None
    is_tax_relevant: Optional[bool] = None
    reminder_date: Optional[datetime] = None
    notes: Optional[str] = None
    correspondent_id: Optional[str] = None
    doctype_id: Optional[str] = None
    tag_ids: Optional[List[str]] = None

class Document(DocumentBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    filename: str
    original_filename: str
    file_hash: str
    file_path: str
    file_size: int
    mime_type: str
    full_text: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None
    ocr_status: str
    ai_status: str
    vector_status: str = "pending"
    view_count: int = 0
    last_viewed: Optional[datetime] = None
    
    # Approval fields
    is_approved: bool = False
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    
    correspondent: Optional[Correspondent] = None
    doctype: Optional[DocType] = None
    tags: List[Tag] = []

# AI Extraction schemas
class AIExtractedData(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    document_date: Optional[str] = None  # Will be parsed to datetime
    correspondent_name: Optional[str] = None
    doctype_name: Optional[str] = None
    tag_names: List[str] = []
    is_tax_relevant: bool = False

class DocumentProcessingStatus(BaseModel):
    document_id: str
    filename: str
    ocr_status: str
    ai_status: str
    extracted_data: Optional[AIExtractedData] = None
    suggestions: Optional[dict] = None

# Search schemas
class SearchFilters(BaseModel):
    correspondent_ids: Optional[List[str]] = None
    doctype_ids: Optional[List[str]] = None
    tag_ids: Optional[List[str]] = None
    date_range: Optional[str] = None  # Predefined time periods
    date_from: Optional[str] = None  # Custom date range start (optional)
    date_to: Optional[str] = None    # Custom date range end (optional)
    is_tax_relevant: Optional[bool] = None
    reminder_filter: Optional[str] = None  # "has", "overdue", or "none"

class SearchRequest(BaseModel):
    query: Optional[str] = None
    filters: Optional[SearchFilters] = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    use_semantic_search: bool = True

class SearchResult(BaseModel):
    documents: List[Document]
    total_count: int
    query: Optional[str] = None
    filters: Optional[SearchFilters] = None

# RAG schemas
class RAGRequest(BaseModel):
    question: str = Field(..., min_length=1)
    filters: Optional[SearchFilters] = None
    max_documents: int = Field(default=5, ge=1, le=20)
    document_ids: Optional[List[str]] = None  # For manual document selection

class RAGResponse(BaseModel):
    answer: str
    sources: List[Document]
    confidence: Optional[float] = None

# File upload schemas
class FileUploadResponse(BaseModel):
    message: str
    document_id: Optional[str] = None
    status: str
    
class StagingFile(BaseModel):
    filename: str
    size: int
    created_at: datetime
    status: str  # pending, processing, completed, error

# Settings schemas
class SettingBase(BaseModel):
    key: str
    value: Optional[str] = None
    description: Optional[str] = None

class SettingCreate(SettingBase):
    pass

class SettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None

class Setting(SettingBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

# Extended Settings schemas
class ExtendedSettingsResponse(BaseModel):
    # AI Provider Settings
    ai_provider: str = "openai"  # "openai" or "azure"
    
    # OpenAI Settings
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-ada-002"
    chat_model: str = "gpt-4o-mini"
    analysis_model: str = "gpt-4o-mini"
    
    # Azure OpenAI Settings
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-06-01"
    azure_openai_chat_deployment: str = ""
    azure_openai_embeddings_deployment: str = ""
    
    # AI Limits
    ai_text_limit: int = 16000
    ai_context_limit: int = 10000
    
    # Database Settings
    database_url: str = "sqlite:///./data/documents.db"
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    chroma_collection_name: str = "documents"
    
    # Folder Settings
    root_folder: str = "."
    staging_folder: str = "./data/staging"
    data_folder: str = "./data"
    storage_folder: str = "./data/storage"
    logs_folder: str = "./data/logs"
    
    # OCR Settings
    tesseract_path: str = "/usr/bin/tesseract"
    poppler_path: str = "/usr/bin"
    
    # File Settings
    max_file_size: str = "100MB"
    allowed_extensions: str = "pdf,png,jpg,jpeg,tiff,bmp,txt,text,md,markdown"
    
    # Security Settings
    secret_key: str = "your-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    
    # Logging
    log_level: str = "INFO"

class ExtendedSettingsUpdate(BaseModel):
    # AI Provider Settings
    ai_provider: Optional[str] = None  # "openai" or "azure"
    
    # OpenAI Settings
    openai_api_key: Optional[str] = None
    embedding_model: Optional[str] = None
    chat_model: Optional[str] = None
    analysis_model: Optional[str] = None
    
    # Azure OpenAI Settings
    azure_openai_api_key: Optional[str] = None
    azure_openai_endpoint: Optional[str] = None
    azure_openai_api_version: Optional[str] = None
    azure_openai_chat_deployment: Optional[str] = None
    azure_openai_embeddings_deployment: Optional[str] = None
    
    # AI Limits
    ai_text_limit: Optional[int] = None
    ai_context_limit: Optional[int] = None
    
    # Database Settings
    database_url: Optional[str] = None
    chroma_host: Optional[str] = None
    chroma_port: Optional[int] = None
    chroma_collection_name: Optional[str] = None
    
    # Folder Settings
    root_folder: Optional[str] = None
    staging_folder: Optional[str] = None
    data_folder: Optional[str] = None
    storage_folder: Optional[str] = None
    logs_folder: Optional[str] = None
    
    # OCR Settings
    tesseract_path: Optional[str] = None
    poppler_path: Optional[str] = None
    
    # File Settings
    max_file_size: Optional[str] = None
    allowed_extensions: Optional[str] = None
    
    # Security Settings
    secret_key: Optional[str] = None
    algorithm: Optional[str] = None
    access_token_expire_minutes: Optional[int] = None
    
    # Logging
    log_level: Optional[str] = None

class ExportConfigResponse(BaseModel):
    include_database: bool = True
    include_documents: bool = True
    include_storage: bool = True
    include_settings: bool = True
    include_vectors: bool = True

class SetupRequest(BaseModel):
    openai_api_key: str
    root_folder: Optional[str] = None
    tesseract_path: Optional[str] = None
    poppler_path: Optional[str] = None

# AI Provider Configuration
class AIProviderConfig(BaseModel):
    provider: str = Field(..., pattern="^(openai|azure)$", description="AI provider: 'openai' or 'azure'")
    
    # OpenAI config
    openai_api_key: Optional[str] = None
    
    # Azure OpenAI config
    azure_api_key: Optional[str] = None
    azure_endpoint: Optional[str] = None
    azure_api_version: Optional[str] = "2024-06-01"
    azure_chat_deployment: Optional[str] = None
    azure_embeddings_deployment: Optional[str] = None

class AIProviderStatus(BaseModel):
    provider: str
    configured: bool
    errors: List[str] = []
    warnings: List[str] = []
    models: dict = {}

# User schemas (for auth)
class UserCreate(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    password: str
    is_admin: bool = False

class UserUpdate(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    password: Optional[str] = None

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    username: str
    email: str
    full_name: Optional[str]
    is_active: bool
    is_admin: bool
    last_login: Optional[datetime]
    created_at: datetime

# Approval schemas
class DocumentApprovalRequest(BaseModel):
    approved: bool

class DocumentApprovalResponse(BaseModel):
    success: bool
    message: str
    document_id: str
    is_approved: bool
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None

# Generic API Response
class APIResponse(BaseModel):
    success: bool
    message: str
    data: Optional[dict] = None
