"""
Thinkion Report Downloader API

A FastAPI implementation that transforms existing logs into JSON format for API consumption.
This approach uses a single source of truth from the main application's logs.
Designed for N8N integration on Railway.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from typing import Optional, Dict, Any, List
import uvicorn
from datetime import datetime
import uuid
import json
import re
import traceback
from pathlib import Path
import threading
import time
import os
import sys
import shutil

from .thinkion_downloader import ThinkionReportDownloader, config
from .bigquery_logger import init_bigquery_logger

# Initialize BigQuery logger
try:
    # Get the path to the credentials file
    credentials_path = Path(__file__).parent.parent / "picante-440315-972b3ea2fd18.json"
    
    # Initialize BigQuery logger
    bigquery_logger = init_bigquery_logger(
        project_id=os.getenv("BIGQUERY_PROJECT_ID", "picante-440315"),
        dataset_id=os.getenv("BIGQUERY_DATASET_ID", "picante"),
        table_id=os.getenv("BIGQUERY_TABLE_ID", "logs"),
        credentials_path=str(credentials_path) if credentials_path.exists() else None,
        batch_size=int(os.getenv("BIGQUERY_BATCH_SIZE", "50")),
        flush_interval=int(os.getenv("BIGQUERY_FLUSH_INTERVAL", "15"))
    )
except Exception as e:
    # Silently continue without BigQuery logging
    pass

# Initialize FastAPI app
app = FastAPI(
    title="Thinkion Report Downloader API",
    description="API for downloading reports from Thinkion POS system",
    version="2.0.0"
)

# Add CORS middleware for N8N integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables for job management
active_jobs: Dict[str, dict] = {}
job_lock = threading.Lock()

# Environment check
def check_environment():
    """Check if the required environment variables and directories are set up correctly."""
    issues = []
    
    # Check Xvfb display
    if not os.environ.get('DISPLAY'):
        issues.append("DISPLAY environment variable not set")
    
    # Check and create necessary directories
    for directory, name in [(config.LOGS_DIR, "logs"), (config.DOWNLOADS_DIR, "downloads")]:
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                issues.append(f"Failed to create {name} directory: {str(e)}")
    
    # Check if we can write to directories
    for directory, name in [(config.LOGS_DIR, "logs"), (config.DOWNLOADS_DIR, "downloads")]:
        test_file = directory / ".test_write"
        try:
            test_file.touch()
            test_file.unlink()
        except Exception as e:
            issues.append(f"Cannot write to {name} directory: {str(e)}")
    
    return issues

# Run environment check at startup
environment_issues = check_environment()

#############################################################################
# Models
#############################################################################

class AccountRequest(BaseModel):
    """Account configuration request model."""
    account_id: int
    store_pos_url: HttpUrl
    store_pos_username: str
    store_pos_password: str
    web_group_selector: str

class JobResponse(BaseModel):
    """Job response model."""
    job_id: str
    status: str
    message: str
    timestamp: datetime
    account_id: int
    progress: Dict[str, str]

class FileDeleteRequest(BaseModel):
    """File deletion request model."""
    filenames: List[str]

class ReportConfig(BaseModel):
    row_number: int
    Thinkion_Id: int
    Report_Type: str
    Report_Id: str
    Report_Name: str
    Report_Url_Param: str
    Report_Columns: str

reports_config: List[ReportConfig] = []

#############################################################################
# Background Job Management
#############################################################################

async def process_download_job(job_id: str, account_data: dict) -> None:
    """Background task to process the download job.
    
    Args:
        job_id: Unique job identifier
        account_data: Account configuration dictionary
    """
    with job_lock:
        active_jobs[job_id] = {
            "status": "running",
            "message": "Job started",
            "timestamp": datetime.now(),
            "account_id": account_data["Account_Id"]
        }
    
    try:
        downloader = ThinkionReportDownloader(
            account_data=[account_data],
            reports_data=config.REPORTS_DATA,
            job_id=job_id,
            wait_seconds=config.WAIT_SECONDS
        )
        
        downloader.run()
        
        with job_lock:
            active_jobs[job_id].update({
                "status": "completed",
                "message": "Download completed successfully",
                "timestamp": datetime.now()
            })
        
    except Exception as e:
        error_msg = str(e)
        with job_lock:
            active_jobs[job_id].update({
                "status": "failed",
                "message": f"Download failed: {error_msg}",
                "timestamp": datetime.now(),
                "error": error_msg
            })

#############################################################################
# API Endpoints
#############################################################################

@app.post("/download", response_model=JobResponse)
async def start_download(
    request: AccountRequest,
    background_tasks: BackgroundTasks
) -> JobResponse:
    """Start a new download job.
    
    Args:
        request: Account configuration
        background_tasks: FastAPI background tasks
        
    Returns:
        Job response with job ID and initial status
    """
    try:
        job_id = str(uuid.uuid4())
        
        # Prepare account data
        account_data = {
            "Account_Id": request.account_id,
            "Store_POS_Url": str(request.store_pos_url),
            "Store_POS_Username": request.store_pos_username,
            "Store_POS_Pass": request.store_pos_password,
            "Web_Group_Selector": request.web_group_selector
        }
        
        # Start background task
        background_tasks.add_task(process_download_job, job_id, account_data)
        
        return JobResponse(
            job_id=job_id,
            status="pending",
            message="Download job created and started",
            timestamp=datetime.now(),
            account_id=request.account_id,
            progress={"stage": "pending", "details": "Job queued for execution"}
        )
        
    except Exception as e:
        error_msg = str(e)
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to create download job: {error_msg}"
        )

@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: str) -> JobResponse:
    """Get the status of a specific job.
    
    Args:
        job_id: Unique job identifier
        
    Returns:
        Job status information
        
    Raises:
        HTTPException: If job is not found
    """
    with job_lock:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job_info = active_jobs[job_id]
        
        return JobResponse(
            job_id=job_id,
            status=job_info["status"],
            message=job_info["message"],
            timestamp=job_info["timestamp"],
            account_id=job_info["account_id"],
            progress=job_info.get("progress", {})
        )

@app.get("/logs/{job_id}")
async def get_job_logs(job_id: str) -> Dict[str, Any]:
    """Get logs for a specific job.
    
    Args:
        job_id: Job identifier
        
    Returns:
        Dictionary containing job logs
    """
    log_file = config.LOGS_DIR / f"{job_id}.log"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="No logs found for this job")
    
    try:
        with open(log_file, 'r') as f:
            logs = f.readlines()
        
        return {
            "job_id": job_id,
            "logs": logs,
            "total_lines": len(logs)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {str(e)}")

@app.get("/files/{job_id}")
async def get_downloaded_files(job_id: str) -> Dict[str, Any]:
    """Get list of files downloaded for a specific job.
    
    Args:
        job_id: Job identifier
        
    Returns:
        Dictionary containing list of downloaded files
    """
    job_dir = config.DOWNLOADS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="No files found for this job")
    
    files = []
    for file_path in job_dir.glob("**/*"):
        if file_path.is_file():
            files.append({
                "name": file_path.name,
                "path": str(file_path.relative_to(config.DOWNLOADS_DIR)),
                "size": file_path.stat().st_size,
                "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
            })
    
    return {
        "job_id": job_id,
        "files": files,
        "total_files": len(files)
    }

@app.get("/files/{job_id}/{filename:path}")
async def download_file(job_id: str, filename: str) -> FileResponse:
    """Download a specific file from a job.
    
    Args:
        job_id: Job identifier
        filename: Name of the file to download
        
    Returns:
        FileResponse with the requested file
    """
    file_path = config.DOWNLOADS_DIR / job_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )

@app.delete("/files/{job_id}")
async def delete_job_files(job_id: str) -> Dict[str, Any]:
    """Delete all files associated with a job.
    
    Args:
        job_id: Job identifier
        
    Returns:
        Dictionary with deletion status
    """
    job_dir = config.DOWNLOADS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="No files found for this job")
    
    try:
        shutil.rmtree(job_dir)
        return {
            "status": "success",
            "message": f"All files for job {job_id} have been deleted",
            "job_id": job_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete files: {str(e)}")

@app.delete("/files/{job_id}/{filename:path}")
async def delete_file(job_id: str, filename: str) -> Dict[str, Any]:
    """Delete a specific file from a job.
    
    Args:
        job_id: Job identifier
        filename: Name of the file to delete
        
    Returns:
        Dictionary with deletion status
    """
    file_path = config.DOWNLOADS_DIR / job_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        file_path.unlink()
        return {
            "status": "success",
            "message": f"File {filename} has been deleted",
            "job_id": job_id,
            "filename": filename
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")

@app.post("/files/{job_id}/delete")
async def delete_multiple_files(job_id: str, request: FileDeleteRequest) -> Dict[str, Any]:
    """Delete multiple specific files from a job.
    
    Args:
        job_id: Job identifier
        request: List of filenames to delete
        
    Returns:
        Dictionary with deletion status for each file
    """
    job_dir = config.DOWNLOADS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="No files found for this job")
    
    results = {
        "job_id": job_id,
        "deleted": [],
        "failed": [],
        "not_found": []
    }
    
    for filename in request.filenames:
        file_path = job_dir / filename
        if not file_path.exists():
            results["not_found"].append(filename)
            continue
            
        try:
            file_path.unlink()
            results["deleted"].append(filename)
        except Exception as e:
            results["failed"].append({
                "filename": filename,
                "error": str(e)
            })
    
    return {
        "status": "success" if not results["failed"] else "partial",
        "results": results
    }

@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint that verifies the application's environment."""
    try:
        # Basic health check
        health_status = {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0.0",
            "environment": {
                "display": os.environ.get('DISPLAY', 'not set'),
                "python_version": sys.version,
                "logs_directory": str(config.LOGS_DIR),
                "logs_writable": os.access(config.LOGS_DIR, os.W_OK),
                "downloads_directory": str(config.DOWNLOADS_DIR),
                "downloads_writable": os.access(config.DOWNLOADS_DIR, os.W_OK)
            }
        }
        
        # Add environment issues if any
        if environment_issues:
            health_status["status"] = "degraded"
            health_status["environment_issues"] = environment_issues
        
        return health_status
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }

@app.post("/config/reports", response_model=Dict[str, Any])
async def update_reports_config(request: List[ReportConfig]):
    """Update the reports configuration from external source (e.g., Google Sheets).
    
    Args:
        request: List of report configurations
        
    Returns:
        Dictionary with update status
    """
    global reports_config
    
    try:
        # Update the global reports configuration
        reports_config = request
        
        # Also update the config in thinkion_downloader
        from .thinkion_downloader import config
        config.REPORTS_DATA = [report.dict() for report in reports_config]
        
        return {
            "reports_config": config.REPORTS_DATA
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update reports configuration: {str(e)}"
        )

@app.get("/config/reports", response_model=List[ReportConfig])
async def get_reports_config():
    """Get the current reports configuration."""
    return reports_config

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))