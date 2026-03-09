import os

import structlog
from app.config import settings
from app.core.rag import ingest_document
from arq import create_pool
from arq.connections import RedisSettings

logger = structlog.get_logger()


async def task_ingest_document(ctx, file_path: str, doc_name: str, org_id: str):
    """
    Background job:ingest document into the org's knowledge base.
    Retried up to 3 times if fails.
    """
    try:
        chunks = await ingest_document(file_path, doc_name, org_id)
        logger.info("Ingest complete", doc_name=doc_name, chunks=chunks, org=org_id)

        if os.path.exists(file_path):
            os.unlink(file_path)
        return {"success": True, "chunks": chunks}
    except Exception as e:
        logger.error("Ingest failed", doc=doc_name, error=str(e))
        raise


class WorkerSettings:
    functions = [task_ingest_document]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 10
    job_timeout = 300
    max_tries = 3


async def enqueue_ingest(file_path: str, doc_name: str, org_id: str):
    pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    await pool.enqueue_job("task_ingest_document", file_path, doc_name, org_id)
    await pool.close()
