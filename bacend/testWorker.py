import asyncio

from app.workers.tasks import enqueue_ingest


async def main():
    await enqueue_ingest("test.pdf", "test_doc", "org_123")


asyncio.run(main())
