from pydantic import BaseModel, Field


class CreateNoteRequest(BaseModel):
    title: str = ""
    body_md: str = ""
    directory_id: int | None = None
    folder_path: str = ""
    source_url: str | None = None
    source_type: str = "manual"
    source_id: str | None = None
    source_meta: dict = Field(default_factory=dict)


class UpdateNoteRequest(BaseModel):
    title: str = ""
    body_md: str = ""
    directory_id: int | None = None
    folder_path: str = ""
    source_url: str | None = None
    source_type: str = "manual"
    source_id: str | None = None
    source_meta: dict = Field(default_factory=dict)


class CreateDirectoryRequest(BaseModel):
    name: str
    parent_id: int | None = None


class RenameDirectoryRequest(BaseModel):
    name: str


class MoveDirectoryRequest(BaseModel):
    parent_id: int | None = None


class MoveNoteRequest(BaseModel):
    directory_id: int | None = None
