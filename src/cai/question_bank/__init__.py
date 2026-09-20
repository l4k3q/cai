"""CAI//OPS 题库学习 - PostgreSQL 持久化层.

本包实现 §7 开发契约中的:
- models.py:  SQLAlchemy ORM (映射已建 PostgreSQL 表)
- schemas.py: Pydantic API 输入输出模型
- repository.py: 异步 PostgreSQL 读写
- materials.py: S3/MinIO 对象存储
- fingerprints.py: 题面规范化与实例指纹
"""

from .models import (
    Question,
    QuestionMaterial,
    RunRecord,
    SolutionMethod,
    SolutionStep,
    BuildJob,
    QuestionResolutionEvent,
    OutboxEvent,
)
from .schemas import (
    QuestionCreate,
    QuestionResponse,
    QuestionBrief,
    MaterialUploadResponse,
    BuildRunResponse,
    SolutionResponse,
    SolutionStepResponse,
    ResolutionResponse,
    LearningContextResponse,
    PaginatedResponse,
)
from .repository import QuestionRepository
from .materials import MaterialStore

__all__ = [
    # ORM
    "Question",
    "QuestionMaterial",
    "RunRecord",
    "SolutionMethod",
    "SolutionStep",
    "BuildJob",
    "QuestionResolutionEvent",
    "OutboxEvent",
    # Schemas
    "QuestionCreate",
    "QuestionResponse",
    "QuestionBrief",
    "MaterialUploadResponse",
    "BuildRunResponse",
    "SolutionResponse",
    "SolutionStepResponse",
    "ResolutionResponse",
    "LearningContextResponse",
    "PaginatedResponse",
    # Services
    "QuestionRepository",
    "MaterialStore",
]
