"""Content understanding (``modules.understanding``, M12): what a title is about, from its own pictures and speech.

On a title's first ``Asset Ready``, one workflow analyses its encode once: Amazon Transcribe (speech, language),
Amazon Rekognition Video (shots, technical cues, labels, moderation) and a Bedrock model through the model registry
(task ``understanding.describe``), which turns the pictures, the transcript and those signals into IAB Tech Lab
Content Taxonomy 3.1 categories, keywords, moods, a brand-safety tier and a synopsis. The result is one item beside
the title's catalog item (:mod:`.record`); the crawl module carries its categories and keywords into ``app.content``
and the recommender reads it.

AWS Elemental Inference, AWS's own video understanding for media, analyses live video only: it has no API for a
file or a VOD asset yet (botocore 1.43.119), so this module glues the general-purpose services together until it
has. The item contract stays when Elemental Inference replaces the Rekognition and model steps.
"""

# OPTIMIZATION(aws): EI-VOD Elemental Inference has no VOD analysis: swap the Rekognition and model steps when it ships

from fast_channels.core.understanding.record import (
    ITEM_SK,
    MAX_CATEGORIES,
    MAX_KEYWORDS,
    MAX_MOODS,
    MOODS,
    SYNOPSIS_MAX_CHARS,
    TASK,
    Analysis,
    BrandSafety,
    Mood,
    Status,
    Understanding,
    item_key,
)

__all__ = [
    "ITEM_SK",
    "MAX_CATEGORIES",
    "MAX_KEYWORDS",
    "MAX_MOODS",
    "MOODS",
    "SYNOPSIS_MAX_CHARS",
    "TASK",
    "Analysis",
    "BrandSafety",
    "Mood",
    "Status",
    "Understanding",
    "item_key",
]
