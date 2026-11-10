"""结果呈现层：图表推荐 / 归因解读 / 追问改写。"""

from app.analysis.chart_router import ChartSpec, route_chart
from app.analysis.followup import FollowupResult, FollowupResolver, TurnContext
from app.analysis.insight import Insight, InsightGenerator

__all__ = [
    "route_chart", "ChartSpec",
    "InsightGenerator", "Insight",
    "FollowupResolver", "TurnContext", "FollowupResult",
]
