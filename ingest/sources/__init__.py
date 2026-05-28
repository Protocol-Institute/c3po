"""
Corpus source modules. Each module exports one class that implements BaseSource.

Usage (from project root):
    from ingest.sources.substack import SubstackSource
    result = SubstackSource().run(dry_run=True)

Phase 1: all classes are subprocess wrappers around the existing ingest scripts.
Phase 2+: replace with direct implementations as scripts are refactored.
"""

from .substack import SubstackSource
from .discord import DiscordSource
from .sig import SIGSource
from .youtube import YouTubeSource
from .pdfs import PDFSource
from .bibliography import BibliographySource
from .definitions import DefinitionsSource
from .discord_links import DiscordLinksSource

__all__ = [
    "SubstackSource",
    "DiscordSource",
    "SIGSource",
    "YouTubeSource",
    "PDFSource",
    "BibliographySource",
    "DefinitionsSource",
    "DiscordLinksSource",
]

REGISTRY: dict[str, type] = {
    "substack": SubstackSource,
    "discord": DiscordSource,
    "sig": SIGSource,
    "youtube": YouTubeSource,
    "pdfs": PDFSource,
    "bibliography": BibliographySource,
    "definitions": DefinitionsSource,
    "discord_links": DiscordLinksSource,
}
