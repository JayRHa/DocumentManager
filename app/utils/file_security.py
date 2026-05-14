"""
File security utilities for access control and permission management.
"""
import os
import stat
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple, List, Dict
try:
    import magic  # python-magic (libmagic)
    MAGIC_AVAILABLE = True
except Exception:
    magic = None
    MAGIC_AVAILABLE = False
from loguru import logger

from ..models import User, Document
from ..config import get_settings
from .validators import validate_safe_path, ValidationError


class FileSecurityError(Exception):
    """Base exception for file security errors"""
    pass


class AccessDeniedError(FileSecurityError):
    """Raised when access to a file is denied"""
    pass


class FileTypeNotAllowedError(FileSecurityError):
    """Raised when file type is not allowed"""
    pass


# Allowed file extensions and MIME types
ALLOWED_EXTENSIONS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.csv', '.rtf', '.odt', '.ods', '.odp'
}

ALLOWED_MIME_TYPES = {
    'application/pdf',
    'image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/tiff',
    'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint', 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain', 'text/csv', 'application/rtf',
    'application/vnd.oasis.opendocument.text',
    'application/vnd.oasis.opendocument.spreadsheet',
    'application/vnd.oasis.opendocument.presentation'
}

# File size limits (in bytes)
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB default
MAX_IMAGE_SIZE = 50 * 1024 * 1024   # 50 MB for images
MAX_DOCUMENT_SIZE = 100 * 1024 * 1024  # 100 MB for documents

# Dangerous file patterns
DANGEROUS_PATTERNS = [
    b'<script', b'javascript:', b'onclick=', b'onerror=',
    b'<?php', b'<%', b'<jsp:', b'#!/bin/',
    b'\x00\x00\x01\x00',  # Windows executable
    b'MZ',  # DOS/Windows executable
    b'\x7fELF',  # Linux ELF executable
]


def check_file_permissions(file_path: Path, user: User) -> bool:
    """
    Check if user has permission to access a file.
    
    Args:
        file_path: Path to the file
        user: User requesting access
        
    Returns:
        True if user has permission, False otherwise
    """
    # Admins have access to all files
    if user.is_admin:
        return True
    
    # Check if file is in allowed directories
    settings = get_settings()
    allowed_dirs = [
        Path(settings.get('storage_path', 'data/storage')),
        Path(settings.get('staging_path', 'data/staging'))
    ]
    
    try:
        # Resolve the file path
        resolved_path = file_path.resolve()
        
        # Check if file is within allowed directories
        for allowed_dir in allowed_dirs:
            try:
                resolved_path.relative_to(allowed_dir.resolve())
                return True
            except ValueError:
                continue
        
        return False
        
    except Exception as e:
        logger.error(f"Error checking file permissions: {e}")
        return False


def validate_file_upload(
    filename: str,
    content: bytes,
    user: User,
    max_size: Optional[int] = None
) -> Tuple[str, str]:
    """
    Validate a file upload for security.
    
    Args:
        filename: Original filename
        content: File content as bytes
        user: User uploading the file
        max_size: Maximum allowed file size (bytes)
        
    Returns:
        Tuple of (safe_filename, mime_type)
        
    Raises:
        FileSecurityError: If file validation fails
    """
    # Validate filename
    if not filename:
        raise FileSecurityError("Filename cannot be empty")
    
    # Get file extension
    file_ext = Path(filename).suffix.lower()
    
    # Check allowed extensions
    if file_ext not in ALLOWED_EXTENSIONS:
        raise FileTypeNotAllowedError(
            f"File type '{file_ext}' not allowed. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    
    # Check file size
    file_size = len(content)
    max_allowed = max_size or MAX_FILE_SIZE
    
    if file_size > max_allowed:
        raise FileSecurityError(
            f"File size ({file_size / 1024 / 1024:.1f}MB) exceeds limit ({max_allowed / 1024 / 1024:.1f}MB)"
        )
    
    # Check MIME type using python-magic when available; fallback otherwise
    try:
        detected_mime = None
        if MAGIC_AVAILABLE:
            mime = magic.Magic(mime=True)
            detected_mime = mime.from_buffer(content)
        else:
            # Basic fallbacks using signatures and stdlib
            try:
                import imghdr
                img_type = imghdr.what(None, h=content[:4096])
                if img_type:
                    detected_mime = mimetypes.types_map.get(f'.{img_type}', f'image/{img_type}')
            except Exception:
                pass
            if not detected_mime and content[:4] == b'%PDF':
                detected_mime = 'application/pdf'
            if not detected_mime:
                detected_mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        if detected_mime not in ALLOWED_MIME_TYPES:
            raise FileTypeNotAllowedError(
                f"MIME type '{detected_mime}' not allowed"
            )

        # Verify MIME type matches extension
        expected_mime = mimetypes.guess_type(filename)[0]
        if expected_mime and expected_mime != detected_mime:
            # Allow some common mismatches
            allowed_mismatches = [
                ('application/octet-stream', 'application/pdf'),
                ('text/plain', 'text/csv'),
            ]
            if (detected_mime, expected_mime) not in allowed_mismatches:
                logger.warning(
                    f"MIME type mismatch: detected={detected_mime}, expected={expected_mime}"
                )
    except FileSecurityError:
        raise
    except Exception as e:
        logger.error(f"Error checking MIME type: {e}")
        raise FileSecurityError("Could not verify file type")
    
    # Check for dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if pattern in content[:1024]:  # Check first 1KB
            raise FileSecurityError("File contains potentially dangerous content")
    
    # Generate safe filename
    safe_filename = sanitize_filename(filename)
    
    return safe_filename, detected_mime


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename to prevent directory traversal and other attacks.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    # Remove path components
    filename = os.path.basename(filename)
    
    # Replace dangerous characters
    dangerous_chars = ['/', '\\', '..', '~', '\x00', '\n', '\r', '\t']
    for char in dangerous_chars:
        filename = filename.replace(char, '_')
    
    # Limit length
    name, ext = os.path.splitext(filename)
    if len(name) > 200:
        name = name[:200]
    
    filename = name + ext
    
    # Ensure filename is not empty
    if not filename or filename.strip() == '':
        filename = 'unnamed_file'
    
    return filename


def secure_file_path(base_path: Path, filename: str) -> Path:
    """
    Create a secure file path preventing directory traversal.
    
    Args:
        base_path: Base directory path
        filename: Filename to append
        
    Returns:
        Secure file path
        
    Raises:
        FileSecurityError: If path would escape base directory
    """
    try:
        # Sanitize filename first
        safe_filename = sanitize_filename(filename)
        
        # Validate path doesn't escape base directory
        validated_path = validate_safe_path(
            safe_filename,
            str(base_path),
            allow_create=True
        )
        
        return validated_path
        
    except ValidationError as e:
        raise FileSecurityError(f"Invalid file path: {str(e)}")


def set_secure_permissions(file_path: Path, is_private: bool = True):
    """
    Set secure file permissions.
    
    Args:
        file_path: Path to the file
        is_private: Whether file should be private (owner-only access)
    """
    try:
        if os.name == 'posix':
            if is_private:
                os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)
            else:
                os.chmod(
                    file_path,
                    stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
                )
        else:
            # On Windows, chmod is limited; skip strict POSIX bits
            pass
    except Exception as e:
        logger.warning(f"Could not set strict file permissions on this platform: {e}")


def calculate_file_hash(file_path: Path, algorithm: str = 'sha256') -> str:
    """
    Calculate cryptographic hash of a file.
    
    Args:
        file_path: Path to the file
        algorithm: Hash algorithm to use
        
    Returns:
        Hex digest of file hash
    """
    hash_func = hashlib.new(algorithm)
    
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            hash_func.update(chunk)
    
    return hash_func.hexdigest()


def check_document_access(
    document: Document,
    user: User,
    permission: str = 'read'
) -> bool:
    """
    Check if user has permission to access a document.
    
    Args:
        document: Document to check
        user: User requesting access
        permission: Permission type (read, write, delete)
        
    Returns:
        True if user has permission
    """
    # Admins have full access
    if user.is_admin:
        return True
    
    # Check user permissions
    user_permissions = set(user.permissions.split(',')) if user.permissions else set()
    
    # Map document permissions to user permissions
    permission_map = {
        'read': 'documents.read',
        'write': 'documents.update',
        'delete': 'documents.delete'
    }
    
    required_permission = permission_map.get(permission, f'documents.{permission}')
    
    return required_permission in user_permissions


def quarantine_file(file_path: Path, reason: str):
    """
    Move a suspicious file to quarantine.
    
    Args:
        file_path: Path to the file
        reason: Reason for quarantine
    """
    try:
        # Create quarantine directory
        quarantine_dir = Path('data/quarantine')
        quarantine_dir.mkdir(exist_ok=True)
        
        # Generate quarantine filename
        timestamp = hashlib.sha256(str(file_path).encode()).hexdigest()[:8]
        quarantine_name = f"{timestamp}_{file_path.name}"
        quarantine_path = quarantine_dir / quarantine_name
        
        # Move file to quarantine
        file_path.rename(quarantine_path)
        
        # Set restrictive permissions
        set_secure_permissions(quarantine_path, is_private=True)
        
        # Log quarantine action
        logger.warning(
            f"File quarantined: {file_path} -> {quarantine_path}, reason: {reason}"
        )
        
        # Create quarantine log
        log_path = quarantine_dir / 'quarantine.log'
        with open(log_path, 'a') as f:
            import datetime
            f.write(
                f"{datetime.datetime.now().isoformat()} | "
                f"{file_path} | {quarantine_path} | {reason}\n"
            )
        
    except Exception as e:
        logger.error(f"Failed to quarantine file: {e}")
        # If quarantine fails, delete the file for safety
        try:
            file_path.unlink()
            logger.warning(f"Deleted suspicious file: {file_path}")
        except Exception:
            pass


def scan_directory_security(directory: Path) -> List[Dict[str, str]]:
    """
    Scan a directory for security issues.
    
    Args:
        directory: Directory to scan
        
    Returns:
        List of security issues found
    """
    issues = []
    
    try:
        for file_path in directory.rglob('*'):
            if file_path.is_file():
                # Check file permissions
                file_stat = file_path.stat()
                mode = file_stat.st_mode
                
                # Check for world-writable files
                if mode & stat.S_IWOTH:
                    issues.append({
                        'file': str(file_path),
                        'issue': 'World-writable file',
                        'severity': 'high'
                    })
                
                # Check for executable files
                if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    issues.append({
                        'file': str(file_path),
                        'issue': 'Executable file found',
                        'severity': 'medium'
                    })
                
                # Check file extension
                if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    issues.append({
                        'file': str(file_path),
                        'issue': f'Disallowed file type: {file_path.suffix}',
                        'severity': 'medium'
                    })
                
    except Exception as e:
        logger.error(f"Error scanning directory: {e}")
        issues.append({
            'file': str(directory),
            'issue': f'Scan error: {str(e)}',
            'severity': 'low'
        })
    
    return issues
