"""
Database Connection Manager
===========================
Async PostgreSQL connection pool with health checks.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional, Any

try:
    import asyncpg
except ImportError:
    asyncpg = None

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages async PostgreSQL connection pool.
    All database operations go through this manager.
    """

    def __init__(self, dsn: str, min_connections: int = 2, max_connections: int = 10):
        self.dsn = dsn
        self.min_connections = min_connections
        self.max_connections = max_connections
        self._pool: Optional[Any] = None

    async def initialize(self) -> None:
        """Create connection pool. Call once at startup."""
        if asyncpg is None:
            raise ImportError("asyncpg is required. Install with: pip install asyncpg")
        
        try:
            self._pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=self.min_connections,
                max_size=self.max_connections,
                command_timeout=30,
            )
            logger.info("Database pool initialized successfully")
        except Exception as e:
            logger.critical(f"Failed to initialize database pool: {e}")
            raise

    async def close(self) -> None:
        """Close connection pool. Call at shutdown."""
        if self._pool:
            await self._pool.close()
            logger.info("Database pool closed")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        if not self._pool:
            raise RuntimeError("Database pool not initialized. Call initialize() first.")
        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a single query."""
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> list:
        """Fetch multiple rows."""
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[Any]:
        """Fetch a single row."""
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Optional[Any]:
        """Fetch a single value."""
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute_many(self, query: str, args_list: list) -> None:
        """Execute a query with multiple parameter sets (batch insert)."""
        async with self.acquire() as conn:
            await conn.executemany(query, args_list)

    async def health_check(self) -> bool:
        """Check database connectivity."""
        try:
            result = await self.fetchval("SELECT 1")
            return result == 1
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False

    async def apply_schema(self, schema_path: str) -> None:
        """Apply SQL schema file to database."""
        with open(schema_path, "r") as f:
            sql = f.read()
        async with self.acquire() as conn:
            await conn.execute(sql)
        logger.info(f"Schema applied from {schema_path}")
