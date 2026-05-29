from pydantic import BaseModel, Field


class ParseLinksRequest(BaseModel):
    urls: list[str] = Field(min_length=1)
    provider: str | None = None


class ParseItemsRequest(BaseModel):
    input: str = Field(min_length=1)
    provider: str | None = None


class ConfirmBookmarkRequest(BaseModel):
    job_id: int
    directory_path: str
    tags: list[str]
    keywords: list[str] = []
    summary: str
    name: str | None = None
    title: str | None = None
