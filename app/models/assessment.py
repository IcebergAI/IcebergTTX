from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class ResponseAssessment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    response_id: int = Field(foreign_key="response.id")
    llm_model: str
    assessment_text: str
    decision_quality: str | None = None          # "good" | "adequate" | "poor"
    recommended_branch_option_id: str | None = None  # maps to a scenario option id
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
