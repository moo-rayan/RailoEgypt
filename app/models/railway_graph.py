from datetime import datetime

from sqlalchemy import Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RailwayGraphData(Base):
    """
    Stores the complete railway graph structure in the database.
    
    This table holds the same data that is cached in Redis, providing
    persistent storage for the railway network graph including nodes,
    edges, spatial grid, and display polylines.
    """
    __tablename__ = "railway_graph_data"
    __table_args__ = {"schema": "EgRailway"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    
    version: Mapped[str] = mapped_column(
        Text, 
        nullable=False, 
        default="1.0",
        comment="Graph version identifier"
    )
    
    data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Complete graph data: nodes, adj, grid, lines"
    )
    
    node_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Total number of nodes in the graph"
    )
    
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, 
        server_default=func.now()
    )
    
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, 
        server_default=func.now(), 
        onupdate=func.now()
    )
