"""Logging configuration for the application."""
import logging
import sys
from src.config import LOG_LEVEL

def setup_logging():
    """Configure application logging."""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('optimizer_system_v2.log')
        ]
    )
    
    return logging.getLogger('optimizer_system_v2')

logger = setup_logging()
