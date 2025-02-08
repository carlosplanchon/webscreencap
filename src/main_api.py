#!/usr/bin/env python3

import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from celery.result import AsyncResult
from celery_task import capture_screenshot_and_html


# FastAPI Setup:
app = FastAPI(
    root_path="/webscreencap",
    title="WebScreenCAP"
)


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

        # If result is None or doesn't exist, handle as not found
        if not result:
            raise HTTPException(
                status_code=404,
                detail="Task not found"
            )

        # Current status
        status = result.status
        # Only show result if it's complete (successful or failure)
        output = result.result if result.ready() else None

        return JSONResponse(
            {
            "task_id": task_id,
            "status": status,
            "result": output
            }
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving task details: {e}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=31137)
