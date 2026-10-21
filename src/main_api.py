#!/usr/bin/env python3

import os
import secrets
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, HttpUrl
from taskiq_redis.exceptions import ResultIsMissingError
from tasks import RESULT_TTL, ScrapeError, broker, capture_screenshot_and_html, redis, task_marker_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    await broker.startup()
    yield
    await broker.shutdown()


# FastAPI Setup:
api = FastAPI(title="WebScreenCAP")


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    """
    Validates the `X-API-Key` header against the `API_KEY` environment variable.
    """
    env_api_key = os.getenv("API_KEY")
    # Constant time, so the key can't be guessed from response times
    if not env_api_key or not api_key or not secrets.compare_digest(
        api_key.encode(), env_api_key.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")


# Models
class TaskInitOut(BaseModel):
    id: str
    message: str


class ScreenshotRequest(BaseModel):
    # Only http(s) URLs; anything else (file://, ...) gets a 422
    url: HttpUrl


@api.post(
    "/api/task/screenshot",
    response_model=TaskInitOut,
    dependencies=[Depends(require_api_key)],
)
async def capture_screenshot_with_api_key(request: ScreenshotRequest):
    """
    Endpoint to initiate a screenshot capture task.
    Requires the `X-API-Key` header.
    """
    try:
        # Remembered before it's queued, since the worker skips the tasks the API doesn't know
        task_id = broker.id_generator()
        await redis.set(task_marker_key(task_id), "1", ex=RESULT_TTL)
        await capture_screenshot_and_html.kicker().with_task_id(task_id).kiq(str(request.url))
        return TaskInitOut(
            id=task_id,
            message="Task started successfully"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Task initiation failed: {e}"
        )


@api.get(
    "/api/task_details/{task_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_task_details_json(task_id: str):
    """
    Endpoint to retrieve task details by task ID.
    Requires the `X-API-Key` header.
    """
    try:
        # Look up the task's result
        try:
            result = await broker.result_backend.get_result(task_id)
        except ResultIsMissingError:
            result = None

        # Unknown (or expired) ids have neither a result nor a marker
        not_found = result is None and not await redis.exists(task_marker_key(task_id))

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task details: {e}"
        )

    if not_found:
        raise HTTPException(
            status_code=404,
            detail="Task not found"
        )

    if result is None:
        status, output = "PENDING", None
    # Only capture errors are meant for clients
    elif result.is_err:
        status = "FAILURE"
        output = {
            "status": "error",
            "message": str(result.error) if isinstance(result.error, ScrapeError) else "Task failed",
        }
    else:
        status, output = "SUCCESS", result.return_value

    return JSONResponse(
        {
        "task_id": task_id,
        "status": status,
        "result": output
        }
    )


# Under /webscreencap, with its docs; mounted apps get no lifespan events, so the broker starts in this one
app = FastAPI(lifespan=lifespan, openapi_url=None)
app.mount("/webscreencap", api)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=31337)
