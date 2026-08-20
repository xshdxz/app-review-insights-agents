from pydantic import BaseModel, Field


class Citation(BaseModel):
    review_id: str
    quote: str


class RagAnswer(BaseModel):
    answer: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    evidence_sufficient: bool = False
    limitation: str = ""
