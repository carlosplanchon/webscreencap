#!/usr/bin/env python3

import os
import secrets
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, HttpUrl
from celery import states
from celery.result import AsyncResult
from celery_task import ScrapeError, celery_app, capture_screenshot_and_html


# FastAPI Setup:
app = FastAPI(
    root_path="/webscreencap",
    title="WebScreenCAP"
)


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


# Celery reports unknown ids as PENDING, so the ones handed out are remembered
def task_marker_key(task_id: str) -> str:
    return f"webscreencap-task-{task_id}"


# Models
class TaskInitOut(BaseModel):
    id: str
    message: str


class ScreenshotRequest(BaseModel):
    # Only http(s) URLs; anything else (file://, ...) gets a 422
    url: HttpUrl


# Plain def: they block on Redis, so FastAPI runs them in its threadpool
@app.post(
    "/api/task/screenshot",
    response_model=TaskInitOut,
    dependencies=[Depends(require_api_key)],
)
def capture_screenshot_with_api_key(request: ScreenshotRequest):
    """
    Endpoint to initiate a screenshot capture task.
    Requires the `X-API-Key` header.
    """
    try:
        # Create Celery task
        task = capture_screenshot_and_html.delay(str(request.url))
        celery_app.backend.set(task_marker_key(task.id), "1")
        return TaskInitOut(
            id=task.id,
            message="Task started successfully"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Task initiation failed: {e}"
        )


@app.get(
    "/api/task_details/{task_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_api_key)],
)
def get_task_details_json(task_id: str):
    """
    Endpoint to retrieve task details by task ID.
    Requires the `X-API-Key` header.
    """
    try:
        # Look up the Celery task
        result = AsyncResult(task_id)

        # Current status
        status = result.status
        # Only show result if it's complete (successful or failure)
        output = result.result if result.ready() else None

        # Unknown (or expired) ids also come back as PENDING
        not_found = (
            status == states.PENDING
            and celery_app.backend.get(task_marker_key(task_id)) is None
        )

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

    # Only capture errors are meant for clients
    if isinstance(output, BaseException):
        output = {
            "status": "error",
            "message": str(output) if isinstance(output, ScrapeError) else "Task failed",
        }

    return JSONResponse(
        {
        "task_id": task_id,
        "status": status,
        "result": output
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=31337)
