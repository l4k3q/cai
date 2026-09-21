"""旁观 Agent（Spectator Agent）核心包.

只读观察会话对话，每满 N 轮评估用户意图；判定"技术求知"时从题库
（仅 usable 方法）检索相关题目生成推荐卡片；用户显式确认后经 API 层
绑定会话，复用既有 explain 教学路径。

设计约束（specs/001-spectator-agent/）：
- 本包禁止 import CAI 模块（保证 Windows 本地可测，宪法 III）
- 外部依赖仅 stdlib + Pydantic；litellm 仅允许在 evaluator.py 的
  LLM 实现方函数体内 lazy import
- observe_round 永不抛出：任何故障静默降级 + JSONL 日志（宪法 II/VI）

测试: uv run --with pytest --with pydantic --with pytest-asyncio pytest spectator/tests/
（工作目录 cai-ops/）
"""

from .config import SpectatorConfig
from .core import CardLifecycle, CardStateError, CooldownPolicy, RoundTrigger
from .evaluator import EvaluationError, IntentEvaluator, LLMIntentEvaluator
from .logging_utils import SpectatorLogger
from .matcher import QuestionSearcher, score_candidates
from .models import (
    CardStatus,
    CooldownState,
    IntentAssessment,
    QuestionDoc,
    RecommendationCandidate,
    SpectatorCard,
    UserAction,
)
from .service import AcceptResult, SpectatorService

__version__ = "0.1.0"

__all__ = [
    "AcceptResult",
    "CardLifecycle",
    "CardStateError",
    "CardStatus",
    "CooldownPolicy",
    "CooldownState",
    "EvaluationError",
    "IntentAssessment",
    "IntentEvaluator",
    "LLMIntentEvaluator",
    "QuestionDoc",
    "QuestionSearcher",
    "RecommendationCandidate",
    "RoundTrigger",
    "SpectatorCard",
    "SpectatorConfig",
    "SpectatorLogger",
    "SpectatorService",
    "UserAction",
    "score_candidates",
]
