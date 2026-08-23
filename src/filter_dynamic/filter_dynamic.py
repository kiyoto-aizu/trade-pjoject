import logging
from src.application.filtering_usecase import FilteringUseCase

logger = logging.getLogger(__name__)

def run(usecase: FilteringUseCase):
    result = usecase.execute()
    logger.info("filtering finished: %d symbols", len(result.symbols))
    return result
