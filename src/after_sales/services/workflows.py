"""One durable workflow dispatch for CLI and the HTTP executor."""

from after_sales.repositories.sqlite import read_database
from after_sales.workflows.durable import PersistentReviewRun
from after_sales.workflows.parallel import ParallelReviewRun, request_cancel


class WorkflowService:
    def __init__(self, repository, settings, **runtime_options):
        self.repository, self.settings = repository, settings
        self.runtime_options = runtime_options

    async def create(self, ticket_id, *, workflow="parallel", **options):
        cls = ParallelReviewRun if workflow == "parallel" else PersistentReviewRun
        return await cls.create(
            self.repository, ticket_id, self.settings, **self.runtime_options, **options
        )

    async def load(self, run_id):
        with read_database(self.repository.path) as connection:
            row = connection.execute(
                "SELECT workflow_version FROM workflow_runtime WHERE run_id=?", (run_id,)
            ).fetchone()
        cls = (
            ParallelReviewRun
            if row and row[0] == ParallelReviewRun.workflow_version
            else PersistentReviewRun
        )
        return await cls.load(self.repository, run_id, self.settings, **self.runtime_options)

    def cancel(self, run_id):
        return request_cancel(self.repository, run_id, self.settings)

    async def execute(self, run_id):
        run = await self.load(run_id)
        try:
            return await run.recover()
        finally:
            await run.aclose()
