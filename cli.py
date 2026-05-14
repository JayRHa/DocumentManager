#!/usr/bin/env python3
"""
Document Management System CLI
Provides command-line interface for the document management system
"""

import argparse
import os
import sys
from pathlib import Path

# Add the app directory to the Python path
sys.path.insert(0, str(Path(__file__).parent))

from app.config import get_settings
from app.database import init_db
from app.services.folder_setup import setup_folders, select_root_folder
from app.services.file_watcher import FileWatcher
from app.services.ai_client_factory import AIClientFactory
from loguru import logger


def init_system():
    """Initialize the document management system"""
    print("🚀 Initializing Document Management System...")
    
    # Initialize database first (create tables)
    print("🗄️  Initializing database...")
    init_db()
    
    # Setup folders (requires database to be initialized)
    print("📁 Setting up folder structure...")
    from app.database import SessionLocal
    with SessionLocal() as db:
        setup_folders(db)
    
    print("✅ System initialized successfully!")
    print("\nNext steps:")
    print("1. Run 'python cli.py serve' to start the web server")
    print("2. Navigate to http://localhost:8000")
    print("3. Configure your AI provider in the Settings tab")
    print("4. Start uploading documents")


def serve():
    """Start the web server"""
    import uvicorn
    from app.database import SessionLocal
    
    with SessionLocal() as db:
        settings = get_settings(db)
    
    print("🌐 Starting Document Management System...")
    print(f"📁 Staging folder: {settings.staging_folder}")
    print(f"💾 Data folder: {settings.data_folder}")
    print(f"📄 Storage folder: {settings.storage_folder}")
    host = os.getenv("DOCUMENT_MANAGER_HOST", "127.0.0.1")
    print("\n🔗 Access the system at: http://localhost:8000")
    print("📚 API documentation at: http://localhost:8000/docs")
    
    uvicorn.run(
        "app.main:app",
        host=host,
        port=8000,
        reload=True,
        log_level="info"
    )


def process_staging():
    """Process all files in the staging folder"""
    print("🔄 Processing files in staging folder...")
    
    from app.database import SessionLocal
    with SessionLocal() as db:
        file_watcher = FileWatcher(db)
    file_watcher.scan_and_process()
    
    print("✅ Staging folder processing completed!")


def status():
    """Show system status"""
    from app.database import SessionLocal
    with SessionLocal() as db:
        settings = get_settings(db)
    
    print("📊 Document Management System Status")
    print("=" * 40)
    
    # Check folders
    folders = [
        ("Staging", settings.staging_folder),
        ("Data", settings.data_folder),
        ("Storage", settings.storage_folder),
        ("Logs", settings.logs_folder)
    ]
    
    for name, path in folders:
        folder_path = Path(path)
        exists = folder_path.exists()
        file_count = len(list(folder_path.glob("*"))) if exists else 0
        
        status_icon = "✅" if exists else "❌"
        print(f"{status_icon} {name}: {path} ({file_count} files)")
    
    # Check database
    try:
        from app.database import SessionLocal
        from app.models import Document
        
        with SessionLocal() as db:
            doc_count = db.query(Document).count()
            pending_ocr = db.query(Document).filter(Document.ocr_status == "pending").count()
            pending_ai = db.query(Document).filter(Document.ai_status == "pending").count()
        
        print(f"\n📄 Documents: {doc_count} total")
        print(f"⏳ Pending OCR: {pending_ocr}")
        print(f"🤖 Pending AI: {pending_ai}")
        
    except Exception as e:
        print(f"❌ Database error: {e}")
    
    # Check AI configuration
    print(f"\n🤖 AI Provider: {settings.ai_provider.upper()}")
    
    config_status = AIClientFactory.validate_configuration(settings)
    if config_status['valid']:
        print(f"✅ {settings.ai_provider.upper()} API: Configured")
    else:
        print(f"❌ {settings.ai_provider.upper()} API: Not configured")
        for error in config_status['errors']:
            print(f"   - {error}")
    
    if config_status['warnings']:
        print("⚠️  Warnings:")
        for warning in config_status['warnings']:
            print(f"   - {warning}")


def setup_root():
    """Setup root folder interactively"""
    print("📁 Select root folder for Document Management System")
    
    root_folder = select_root_folder()
    if root_folder:
        print(f"✅ Root folder selected: {root_folder}")
        
        # Save to database
        from app.database import SessionLocal
        from app.models import Settings as SettingsModel
        
        with SessionLocal() as db:
            setting = db.query(SettingsModel).filter(SettingsModel.key == "root_folder").first()
            if setting:
                setting.value = root_folder
            else:
                setting = SettingsModel(key="root_folder", value=root_folder, description="Root folder for documents")
                db.add(setting)
            db.commit()
        
        print("✅ Root folder saved to database configuration")
    else:
        print("❌ No folder selected")


def reindex_vectors(force=False):
    """Re-index documents in the vector database
    
    Args:
        force: If True, reindex all documents. If False, only reindex documents with vector_status != 'completed'
    """
    if force:
        print("🔄 Force re-indexing ALL documents in vector database...")
    else:
        print("🔄 Re-indexing documents that need vector indexing...")
    
    try:
        from app.database import SessionLocal
        from app.models import Document
        from app.services.vector_db_service import VectorDBService
        from app.services.ai_service import AIService
        
        # Initialize services
        with SessionLocal() as db:
            vector_db = VectorDBService(db)
            ai_service = AIService(db_session=db)
        
        # Get documents to index
        with SessionLocal() as db:
            if force:
                # Clear existing collection for force reindex
                print("Clearing existing vector collection...")
                vector_db.reset_collection()
                
                # Reset all vector statuses
                db.query(Document).update({Document.vector_status: "pending"})
                db.commit()
                
                # Get all documents with text
                documents = db.query(Document).filter(Document.full_text.isnot(None)).all()
            else:
                # Get only documents that need indexing
                documents = db.query(Document).filter(
                    Document.full_text.isnot(None),
                    Document.vector_status != "completed"
                ).all()
            
            total_docs = len(documents)
            
            if total_docs == 0:
                print("✅ No documents need indexing.")
                return
            
            print(f"Found {total_docs} documents to index")
            
            # Re-index each document
            success_count = 0
            error_count = 0
            
            for i, doc in enumerate(documents, 1):
                try:
                    print(f"Processing {i}/{total_docs}: {doc.filename}...", end="", flush=True)
                    
                    # Prepare text for embedding
                    embedding_parts = []
                    
                    # Title gets more weight by repeating it
                    if doc.title:
                        embedding_parts.extend([doc.title] * 3)
                    
                    # Summary is important for semantic search
                    if doc.summary:
                        embedding_parts.append(doc.summary)
                    
                    # Full text provides comprehensive context
                    if doc.full_text:
                        embedding_parts.append(doc.full_text[:5000])  # Limit to 5000 chars
                    
                    # Add correspondent name for better filtering
                    if doc.correspondent and doc.correspondent.name:
                        embedding_parts.append(f"Correspondent: {doc.correspondent.name}")
                    
                    # Add tags for searchability
                    if doc.tags:
                        tag_names = [tag.name for tag in doc.tags]
                        embedding_parts.append(f"Tags: {', '.join(tag_names)}")
                    
                    # Combine all parts
                    text_for_embedding = "\n".join(embedding_parts)
                    
                    if not text_for_embedding.strip():
                        print(" ⚠️  No text content, skipping")
                        continue
                    
                    # Generate embeddings
                    embeddings = ai_service.generate_embeddings(text_for_embedding)
                    
                    # Prepare metadata
                    metadata = {
                        "document_id": doc.id,
                        "title": doc.title or doc.filename,
                        "correspondent": doc.correspondent.name if doc.correspondent else None,
                        "doctype": doc.doctype.name if doc.doctype else None,
                        "is_tax_relevant": doc.is_tax_relevant,
                        "created_at": doc.created_at.isoformat()
                    }
                    
                    # Store in vector database
                    vector_db.add_document(
                        document_id=doc.id,
                        text=text_for_embedding,
                        embeddings=embeddings,
                        metadata=metadata
                    )
                    
                    # Update document status
                    doc.vector_status = "completed"
                    db.commit()
                    
                    success_count += 1
                    print(" ✅")
                    
                except Exception as e:
                    error_count += 1
                    print(f" ❌ Error: {str(e)}")
                    logger.error(f"Failed to index document {doc.id}: {e}")
                    
                    # Update document status
                    doc.vector_status = "failed"
                    db.commit()
            
            print("\n✅ Re-indexing completed!")
            print(f"   Successfully indexed: {success_count}")
            print(f"   Failed: {error_count}")
            
            # Show collection stats
            stats = vector_db.get_collection_stats()
            print(f"   Total documents in vector DB: {stats['document_count']}")
            
    except Exception as e:
        print(f"❌ Error during re-indexing: {e}")
        logger.error(f"Re-indexing failed: {e}")


def handle_db_command(args):
    """Handle database management commands"""
    if not args.db_command:
        print("Please specify a database command. Use 'cli.py db --help' for options.")
        return
    
    from app.database import get_db
    from app.utils.database_optimization import create_indexes, analyze_database, optimize_database, get_database_size
    
    with next(get_db()) as db:
        if args.db_command == "create-indexes":
            print("🔧 Creating database indexes...")
            try:
                results = create_indexes(db)
                
                created_count = sum(1 for r in results if r.get('status') == 'created')
                failed_count = sum(1 for r in results if r.get('status') == 'failed')
                
                print(f"✅ Created {created_count} indexes")
                if failed_count > 0:
                    print(f"❌ Failed to create {failed_count} indexes")
                    for result in results:
                        if result.get('status') == 'failed':
                            print(f"   - {result.get('name', 'unknown')}: {result.get('error')}")
                
            except Exception as e:
                print(f"❌ Error creating indexes: {e}")
        
        elif args.db_command == "analyze":
            print("📊 Analyzing database performance...")
            try:
                analysis = analyze_database(db)
                
                print("\n📈 Table Statistics:")
                for stat in analysis.get('table_stats', []):
                    print(f"   {stat['table']}: {stat['row_count']:,} rows")
                
                print(f"\n🔍 Found {len(analysis.get('index_usage', []))} indexes")
                
                print("\n💡 Recommendations:")
                for rec in analysis.get('recommendations', []):
                    priority_icon = "🔴" if rec['priority'] == 'high' else "🟡" if rec['priority'] == 'medium' else "🟢"
                    print(f"   {priority_icon} {rec['recommendation']}")
                    print(f"      Reason: {rec['reason']}")
                
            except Exception as e:
                print(f"❌ Error analyzing database: {e}")
        
        elif args.db_command == "optimize":
            print("⚡ Optimizing database...")
            try:
                results = optimize_database(db)
                
                if results.get('vacuum'):
                    print("✅ VACUUM completed")
                if results.get('analyze'):
                    print("✅ ANALYZE completed")
                if results.get('reindex'):
                    print("✅ REINDEX completed")
                
                if results.get('errors'):
                    print("❌ Errors during optimization:")
                    for error in results['errors']:
                        print(f"   - {error}")
                
            except Exception as e:
                print(f"❌ Error optimizing database: {e}")
        
        elif args.db_command == "size":
            print("📏 Getting database size information...")
            try:
                size_info = get_database_size()
                
                if 'total_size_mb' in size_info:
                    print(f"\n💾 Total Database Size: {size_info['total_size_mb']} MB")
                
                if 'tables' in size_info:
                    print("\n📊 Table Sizes:")
                    for table, info in size_info['tables'].items():
                        if 'size_mb' in info:
                            print(f"   {table}: {info['size_mb']} MB")
                        elif 'estimated_size_kb' in info:
                            print(f"   {table}: {info['estimated_size_kb']} KB ({info['row_count']} rows)")
                
            except Exception as e:
                print(f"❌ Error getting database size: {e}")


def handle_backup_command(args):
    """Handle backup management commands"""
    if not args.backup_command:
        print("Please specify a backup command. Use 'cli.py backup --help' for options.")
        return
    
    from app.database import get_db
    from app.utils.backup import create_backup, restore_backup, list_backups
    from pathlib import Path
    
    if args.backup_command == "create":
        print("💾 Creating system backup...")
        try:
            with next(get_db()) as db:
                backup_info = create_backup(
                    db_session=db,
                    backup_name=args.name,
                    include_files=not args.no_files
                )
                
                print("✅ Backup created successfully!")
                print(f"   Name: {backup_info['name']}")
                print(f"   Archive: {backup_info.get('archive_path')}")
                print(f"   Size: {backup_info.get('archive_size_mb', 0)} MB")
                
                if backup_info.get('errors'):
                    print("❌ Errors during backup:")
                    for error in backup_info['errors']:
                        print(f"   - {error}")
        
        except Exception as e:
            print(f"❌ Backup failed: {e}")
    
    elif args.backup_command == "restore":
        archive_path = Path(args.archive)
        if not archive_path.exists():
            print(f"❌ Backup archive not found: {archive_path}")
            return
        
        print(f"🔄 Restoring backup from {archive_path}...")
        
        # Confirm restore
        response = input("⚠️  This will overwrite existing data. Continue? (y/N): ")
        if response.lower() != 'y':
            print("Restore cancelled.")
            return
        
        try:
            with next(get_db()) as db:
                restore_info = restore_backup(
                    archive_path=archive_path,
                    db_session=db,
                    restore_files=not args.no_files
                )
                
                print("✅ Restore completed successfully!")
                if restore_info.get('database_restored'):
                    print("   ✅ Database restored")
                if restore_info.get('files_restored'):
                    print("   ✅ Files restored")
                
                if restore_info.get('errors'):
                    print("❌ Errors during restore:")
                    for error in restore_info['errors']:
                        print(f"   - {error}")
        
        except Exception as e:
            print(f"❌ Restore failed: {e}")
    
    elif args.backup_command == "list":
        print("📋 Available backups:")
        try:
            backups = list_backups()
            
            if not backups:
                print("   No backups found.")
                return
            
            for backup in backups:
                print(f"\n📦 {backup['filename']}")
                print(f"   Size: {backup['size_mb']} MB")
                if backup.get('created_at'):
                    print(f"   Created: {backup['created_at']}")
                if backup.get('created_by'):
                    print(f"   By: {backup['created_by']}")
                if backup.get('statistics'):
                    stats = backup['statistics']
                    print(f"   Documents: {stats.get('total_documents', 0)}")
                    print(f"   Users: {stats.get('total_users', 0)}")
                if backup.get('error'):
                    print(f"   ❌ Error: {backup['error']}")
        
        except Exception as e:
            print(f"❌ Error listing backups: {e}")


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(description="Document Management System CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Init command
    subparsers.add_parser("init", help="Initialize the system")
    
    # Serve command
    subparsers.add_parser("serve", help="Start the web server")
    
    # Process command
    subparsers.add_parser("process", help="Process files in staging folder")
    
    # Status command
    subparsers.add_parser("status", help="Show system status")
    
    # Setup command
    subparsers.add_parser("setup-root", help="Setup root folder")
    
    # Reindex vectors command
    reindex_parser = subparsers.add_parser("reindex-vectors", help="Re-index documents in the vector database")
    reindex_parser.add_argument("--force", action="store_true", help="Force reindex all documents")
    
    # Database optimization commands
    db_parser = subparsers.add_parser("db", help="Database management commands")
    db_subparsers = db_parser.add_subparsers(dest="db_command", help="Database operations")
    
    # Index management
    db_subparsers.add_parser("create-indexes", help="Create database indexes for performance")
    db_subparsers.add_parser("analyze", help="Analyze database performance")
    db_subparsers.add_parser("optimize", help="Optimize database (VACUUM, ANALYZE, REINDEX)")
    db_subparsers.add_parser("size", help="Show database size information")
    
    # Backup commands
    backup_parser = subparsers.add_parser("backup", help="Backup management commands")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command", help="Backup operations")
    
    create_backup_parser = backup_subparsers.add_parser("create", help="Create system backup")
    create_backup_parser.add_argument("--name", help="Custom backup name")
    create_backup_parser.add_argument("--no-files", action="store_true", help="Skip file backup")
    
    restore_backup_parser = backup_subparsers.add_parser("restore", help="Restore system backup")
    restore_backup_parser.add_argument("archive", help="Path to backup archive")
    restore_backup_parser.add_argument("--no-files", action="store_true", help="Skip file restore")
    
    backup_subparsers.add_parser("list", help="List available backups")
    
    args = parser.parse_args()
    
    if args.command == "init":
        init_system()
    elif args.command == "serve":
        serve()
    elif args.command == "process":
        process_staging()
    elif args.command == "status":
        status()
    elif args.command == "setup-root":
        setup_root()
    elif args.command == "reindex-vectors":
        reindex_vectors(force=args.force)
    elif args.command == "db":
        handle_db_command(args)
    elif args.command == "backup":
        handle_backup_command(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
