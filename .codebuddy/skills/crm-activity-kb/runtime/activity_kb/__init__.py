"""Local activity knowledge base for the CRM attribution agent."""

from .search import search_activities, search_activities_batch

__all__ = ["search_activities", "search_activities_batch"]
__version__ = "1.0.0"
