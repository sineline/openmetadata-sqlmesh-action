"""Standalone SQLMesh metadata and execution reporting for OpenMetadata."""

from .audits import AuditRecord, publish_audits
from .client import APIError, OpenMetadataClient
from .config import Config
from .extract import extract_context, load_project
from .models import ColumnLineage, ModelMetadata, ProjectMetadata
from .names import TableName
from .observer import SQLMeshObserver, install, uninstall
from .sync import Synchronizer, SyncReport, sync_context

__version__ = "0.1.0"
__all__ = [
    "APIError",
    "AuditRecord",
    "ColumnLineage",
    "Config",
    "ModelMetadata",
    "OpenMetadataClient",
    "ProjectMetadata",
    "SQLMeshObserver",
    "SyncReport",
    "Synchronizer",
    "TableName",
    "extract_context",
    "install",
    "load_project",
    "publish_audits",
    "sync_context",
    "uninstall",
]
