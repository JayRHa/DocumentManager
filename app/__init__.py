# Application package initialization
from pathlib import Path
from loguru import logger

# Configure loguru to write logs to the logs directory
log_dir = Path(__file__).parent.parent / "data" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

# Remove default logger
logger.remove()

# Add console logger
logger.add(
    sink=lambda msg: print(msg, end=""),
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} - {message}",
    level="INFO"
)

# Add file logger for server logs
logger.add(
    sink=log_dir / "server.log",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} - {message}",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    compression="zip"
)
