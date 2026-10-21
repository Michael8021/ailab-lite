"""Import every service entrypoint (smoke test after deploy)."""

from ailab_platform.agent.main import app as agent_app
from ailab_platform.api.main import app as api_app
from ailab_platform.broker.main import app as broker_app
from ailab_platform.worker.main import app as worker_app

assert api_app and agent_app and broker_app and worker_app
print("IMPORTS_OK")
