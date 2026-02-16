"""Centralized Logging Configuration for Episeerr"""
import os
import logging
from logging.handlers import RotatingFileHandler

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
VALID_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if LOG_LEVEL not in VALID_LEVELS:
    LOG_LEVEL = 'INFO'

LOG_LEVEL_INT = getattr(logging, LOG_LEVEL)
LOG_DIR = os.getenv('LOG_DIR', '/app/logs')
os.makedirs(LOG_DIR, exist_ok=True)

MAIN_LOG = os.path.join(LOG_DIR, 'episeerr.log')

def setup_main_logger(name='episeerr'):
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL_INT)
    logger.handlers.clear()
    
    file_handler = RotatingFileHandler(MAIN_LOG, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setLevel(LOG_LEVEL_INT)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(max(LOG_LEVEL_INT, logging.INFO))
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger

main_logger = setup_main_logger()