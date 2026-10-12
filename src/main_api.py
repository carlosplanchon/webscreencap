#!/usr/bin/env python3

import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from celery import states
from celery.result import AsyncResult
from celery_task import celery_app, capture_screenshot_and_html


# FastAPI Setup:
app = FastAPI(
    root_path="/webscreencap",
    title="WebScreenCAP"
)


# Celery reports any unknown task id as PENDING, so the ids we hand out are
# remembered (with the same TTL as the results) to tell "queued" from "not found".
def task_marker_key(task_id: str) -> str:
    return f"webscreencap-task-{task_id}"


# Models
class TaskOut(BaseModel):
    id: str
    status: str


class TaskInitOut(BaseModel):
    id: str
    message: str


class ScreenshotRequest(BaseModel):
    url: str


@app.get("/api/task/screenshot", response_model=TaskInitOut)
async def capture_screenshot_with_api_key(url: str, api_key: str):
    """
    Endpoint to initiate a screenshot capture task.
    Validates the api_key against a predefined environment variable `API_KEY`.
    """
    # Check environment variable
    env_api_key = os.getenv("API_KEY")
    if not env_api_key or api_key != env_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    try:
        # Create Celery task
        task = capture_screenshot_and_html.delay(url)
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


@app.get("/api/task_details/{task_id}", response_class=JSONResponse)
async def get_task_details_json(task_id: str, api_key: str):
    """
    Endpoint to retrieve task details by task ID.
    Validates the api_key against a predefined environment variable `API_KEY`.
    """
    env_api_key = os.getenv("API_KEY")
    if not env_api_key or api_key != env_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

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

    # Failed or revoked tasks hold an exception, which isn't JSON serializable
    if isinstance(output, BaseException):
        output = {
            "status": "error",
            "message": "Task failed",
            "error": repr(output),
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
    uvicorn.run(app, host="127.0.0.1", port=31137)
