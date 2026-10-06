"""科技战略协作基础服务的服务端基础包。"""

from .milestone_service import MilestoneService
from .service import DomainService

__all__ = ["DomainService", "MilestoneService"]
