import logging
import os
from pathlib import Path

# Create logs directory if it doesn't exist
logs_dir = Path("logs")
logs_dir.mkdir(exist_ok=True)

# Configure logging before importing app
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        # Console handler
        logging.StreamHandler(),
        # File handler
        logging.FileHandler(logs_dir / "delta_hedger.log")
    ]
)

# Get logger for this module
logger = logging.getLogger(__name__)

from app import app

if __name__ == "__main__":
    logger.info("Starting Delta Hedging Platform")
    app.run(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", 8000)),
        debug=os.getenv("FLASK_DEBUG", "True").lower() == "true"
    )