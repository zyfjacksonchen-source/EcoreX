"""Backend-authoritative local project catalog for the WebUI."""

from .picker import FolderPicker, ProjectFolderSelectionCancelled, pick_project_folder
from .service import ProjectNotFound, ProjectService
from .workspace import ProjectWorkspaceAuthority

__all__ = [
    "FolderPicker",
    "ProjectFolderSelectionCancelled",
    "ProjectNotFound",
    "ProjectService",
    "ProjectWorkspaceAuthority",
    "pick_project_folder",
]
