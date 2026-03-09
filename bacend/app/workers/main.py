from app.workers.tasks import WorkerSettings
from arq import run_worker

if __name__ == "__main__":
    print("worker")
    run_worker(WorkerSettings)
