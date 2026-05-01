from .pipeline import process_transparent_request
from .types import (
    BackgroundRemovalProvider,
    BackgroundRemovalResult,
    TransparentPipelineFailure,
    TransparentPipelineOutput,
    TransparentQcReport,
)

__all__ = [
    "BackgroundRemovalProvider",
    "BackgroundRemovalResult",
    "TransparentPipelineFailure",
    "TransparentPipelineOutput",
    "TransparentQcReport",
    "process_transparent_request",
]
