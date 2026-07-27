from loguru import logger
import pytesseract
from PIL import Image
try:
    import pdf2image
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    # Logger is available now; warn gracefully
    logger.warning("pdf2image not available. PDF OCR for PDFs will be limited.")
from pathlib import Path
from typing import Optional
import tempfile
import os
from sqlalchemy.orm import Session
from ..config import get_settings

class OCRService:
    def __init__(self, db: Session = None):
        self.settings = get_settings(db)
        self._setup_tesseract()
    
    def _setup_tesseract(self):
        """Setup Tesseract OCR configuration"""
        # Try multiple possible paths for tesseract across platforms
        possible_paths = [
            # macOS (Homebrew)
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
            # Windows typical installs
            r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
            r"C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
            # Fallback to PATH
            "tesseract",
        ]
        
        # If a specific path is configured, try it first
        if self.settings.tesseract_path and self.settings.tesseract_path != "/usr/bin/tesseract":
            possible_paths.insert(0, self.settings.tesseract_path)
        
        tesseract_cmd = None
        
        # Test each path
        for path in possible_paths:
            try:
                import subprocess
                result = subprocess.run([path, "--version"], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    tesseract_cmd = path
                    logger.info(f"Found working tesseract at: {path}")
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
        
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            logger.info(f"Using tesseract: {tesseract_cmd}")
        else:
            logger.error("No working tesseract installation found")
            # Try to provide helpful error message per OS
            if os.name == "nt":
                logger.error("Windows install: winget install tesseract-ocr or choco install tesseract")
            else:
                logger.error("macOS: brew install tesseract | Linux: apt-get install tesseract-ocr")
    
    def extract_text_from_image(self, image_path: Path) -> str:
        """Extract text from an image file"""
        try:
            with Image.open(image_path) as image:
                # Configure OCR for better accuracy
                config = '--oem 3 --psm 6'
                text = pytesseract.image_to_string(image, config=config)
                
                logger.info(f"Successfully extracted text from image: {image_path.name}")
                return text.strip()
                
        except Exception as e:
            logger.error(f"Failed to extract text from image {image_path}: {e}")
            raise
    
    def extract_text_from_pdf(self, pdf_path: Path) -> str:
        """Extract text from a PDF file"""
        if not PDF2IMAGE_AVAILABLE:
            logger.error("pdf2image is not available. Please install poppler and pdf2image")
            if os.name == "nt":
                logger.error("Windows poppler: choco install poppler or download binaries and set Settings.poppler_path to bin folder")
            else:
                logger.error("macOS: brew install poppler | Linux: apt-get install poppler-utils")
            raise RuntimeError("PDF OCR requires poppler. Install poppler and set Settings.poppler_path if needed.")
            
        try:
            all_text = []
            
            # Convert PDF pages to images
            # Determine poppler path if provided or auto-detect on Windows
            poppler_path = None
            if getattr(self.settings, "poppler_path", None):
                poppler_path = self.settings.poppler_path
            elif os.name == "nt":
                # Common Windows poppler locations
                common_poppler_bins = [
                    r"C:\\Program Files\\poppler\\Library\\bin",
                    r"C:\\Program Files\\poppler-23.11.0\\Library\\bin",
                    r"C:\\Program Files\\poppler-0.68.0\\bin",
                ]
                for p in common_poppler_bins:
                    if Path(p).exists():
                        poppler_path = p
                        logger.info(f"Using poppler at: {p}")
                        break
            
            pages = pdf2image.convert_from_path(pdf_path, poppler_path=poppler_path)
            
            for i, page in enumerate(pages):
                logger.debug(f"Processing page {i+1} of {pdf_path.name}")
                
                # Save page as temporary image
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                    page.save(temp_file.name, 'PNG')
                    temp_path = Path(temp_file.name)
                
                try:
                    # Extract text from page image
                    page_text = self.extract_text_from_image(temp_path)
                    if page_text:
                        all_text.append(page_text)
                finally:
                    # Clean up temporary file
                    if temp_path.exists():
                        temp_path.unlink()
            
            full_text = "\n\n".join(all_text)
            logger.info(f"Successfully extracted text from PDF: {pdf_path.name} ({len(pages)} pages)")
            return full_text
            
        except Exception as e:
            logger.error(f"Failed to extract text from PDF {pdf_path}: {e}")
            raise
    
    def extract_text_from_text_file(self, text_path: Path) -> str:
        """Extract text from a plain text file"""
        try:
            # Try different encodings
            encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']
            
            for encoding in encodings:
                try:
                    with open(text_path, 'r', encoding=encoding) as file:
                        text = file.read()
                        logger.info(f"Successfully read text file: {text_path.name} (encoding: {encoding})")
                        return text.strip()
                except UnicodeDecodeError:
                    continue
            
            # If all encodings fail, try with error handling
            with open(text_path, 'r', encoding='utf-8', errors='replace') as file:
                text = file.read()
                logger.warning(f"Read text file with character replacement: {text_path.name}")
                return text.strip()
                
        except Exception as e:
            logger.error(f"Failed to read text file {text_path}: {e}")
            raise

    def extract_text(self, file_path: Path) -> str:
        """Extract text from a file based on its type"""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_extension = file_path.suffix.lower()
        
        try:
            if file_extension == '.pdf':
                return self.extract_text_from_pdf(file_path)
            elif file_extension in ['.png', '.jpg', '.jpeg', '.tiff', '.bmp']:
                return self.extract_text_from_image(file_path)
            elif file_extension in ['.txt', '.text', '.md', '.markdown']:
                return self.extract_text_from_text_file(file_path)
            else:
                raise ValueError(f"Unsupported file type: {file_extension}")
                
        except Exception as e:
            logger.error(f"Text extraction failed for {file_path}: {e}")
            raise
    
    def get_ocr_confidence(self, file_path: Path) -> Optional[float]:
        """Get OCR confidence score for a file"""
        try:
            if file_path.suffix.lower() == '.pdf':
                # For PDFs, we'd need to process each page and average confidence
                # This is a simplified implementation
                return None
            else:
                with Image.open(file_path) as image:
                    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
                    confidences = [int(conf) for conf in data['conf'] if int(conf) > 0]
                    
                    if confidences:
                        avg_confidence = sum(confidences) / len(confidences)
                        return avg_confidence / 100.0  # Convert to 0-1 scale
                    
                    return None
                    
        except Exception as e:
            logger.warning(f"Could not calculate OCR confidence for {file_path}: {e}")
            return None
