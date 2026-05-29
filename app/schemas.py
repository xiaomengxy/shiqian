from pydantic import BaseModel, Field


class ParseLinksRequest(BaseModel):
    urls: list[str] = Field(min_length=1)
    provider: str | None = None


class ParseItemsRequest(BaseModel):
    input: str = Field(min_length=1)
    provider: str | None = None
    grouping_mode: str = "smart"


class ConfirmBookmarkRequest(BaseModel):
    job_id: int
    directory_path: str
    tags: list[str]
    keywords: list[str] = []
    summary: str
    name: str | None = None
    title: str | None = None


class UpdateBookmarkRequest(BaseModel):
    name: str
    summary: str
    directory_path: str
    tags: list[str] = []
    keywords: list[str] = []
    raw_input: str = ""
    url: str | None = None


class SummaryRewriteRequest(BaseModel):
    length: str = "normal"
    style: str = "note"
    current_summary: str = ""
    provider: str | None = None
