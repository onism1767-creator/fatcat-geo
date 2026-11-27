"""各契约共用的基类与取值。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


PageRole = Literal["given", "home", "discovered"]
