from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PaperSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: NonEmptyText
    objective: NonEmptyText
    method: NonEmptyText
    results: list[NonEmptyText]
    limitations: list[NonEmptyText]
