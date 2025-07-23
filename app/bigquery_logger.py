"""
BigQuery Logger for Thinkion Report Downloader

Handles logging to Google BigQuery with batching, error handling, and retry logic.
"""

import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, List
from google.cloud import bigquery
from google.api_core import retry
from google.api_core.exceptions import GoogleAPIError
import threading


class BigQueryLogger:
    """BigQuery logger for storing job logs in Google BigQuery."""
    
    def __init__(
        self,
        project_id: str = "picante-440315",
        dataset_id: str = "picante",
        table_id: str = "logs",
        credentials_path: Optional[str] = None,
        batch_size: int = 100,
        flush_interval: int = 30
    ):
        """Initialize BigQuery logger.
        
        Args:
            project_id: Google Cloud project ID
            dataset_id: BigQuery dataset ID
            table_id: BigQuery table ID
            credentials_path: Path to service account JSON file
            batch_size: Number of logs to batch before sending
            flush_interval: Seconds to wait before flushing batch
        """
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.credentials_path = credentials_path
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        
        # Initialize BigQuery client
        self._init_client()
        
        # Batch storage
        self.batch: List[Dict[str, Any]] = []
        self.batch_lock = threading.Lock()
        
        # Start background flush thread
        self.flush_thread = threading.Thread(target=self._background_flush, daemon=True)
        self.flush_thread.start()
        
        # Track last flush time
        self.last_flush = time.time()
    
    def _init_client(self) -> None:
        """Initialize BigQuery client."""
        try:
            if self.credentials_path:
                self.client = bigquery.Client.from_service_account_json(
                    self.credentials_path,
                    project=self.project_id
                )
            else:
                self.client = bigquery.Client(project=self.project_id)
            
            # Construct table reference
            self.table_ref = f"{self.project_id}.{self.dataset_id}.{self.table_id}"
            
        except Exception as e:
            logging.error(f"Failed to initialize BigQuery client: {str(e)}")
            self.client = None
            self.table_ref = None
    
    def log(
        self,
        job_id: str,
        level: str,
        message: str,
        task_id: Optional[str] = None,
        account_id: Optional[str] = None,
        report_name: Optional[str] = None,
        error_details: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a message to BigQuery.
        
        Args:
            job_id: Unique job identifier
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            task_id: Optional task identifier
            account_id: Optional account identifier
            report_name: Optional report name
            error_details: Optional error details
            metadata: Optional metadata dictionary
        """
        if not self.client or not self.table_ref:
            return
        
        # Create log entry
        log_entry = {
            "job_id": job_id,
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "account_id": account_id,
            "report_name": report_name,
            "error_details": error_details,
            "metadata": json.dumps(metadata) if metadata else None,
            "created_at": datetime.now().isoformat()
        }
        
        # Add to batch
        with self.batch_lock:
            self.batch.append(log_entry)
            
            # Flush if batch is full
            if len(self.batch) >= self.batch_size:
                self._flush_batch()
    
    def _flush_batch(self) -> None:
        """Flush the current batch to BigQuery."""
        if not self.batch:
            return
        
        with self.batch_lock:
            batch_to_send = self.batch.copy()
            self.batch.clear()
        
        if not batch_to_send:
            return
        
        try:
            # Insert rows into BigQuery
            errors = self.client.insert_rows_json(
                self.table_ref,
                batch_to_send,
                retry=retry.Retry(deadline=30)
            )
            
            if errors:
                logging.error(f"BigQuery insert errors: {errors}")
                # Re-add failed entries to batch for retry
                with self.batch_lock:
                    self.batch.extend(batch_to_send)
            else:
                logging.debug(f"Successfully inserted {len(batch_to_send)} log entries to BigQuery")
                
        except GoogleAPIError as e:
            logging.error(f"BigQuery API error: {str(e)}")
            # Re-add entries to batch for retry
            with self.batch_lock:
                self.batch.extend(batch_to_send)
        except Exception as e:
            logging.error(f"Unexpected error inserting to BigQuery: {str(e)}")
            # Re-add entries to batch for retry
            with self.batch_lock:
                self.batch.extend(batch_to_send)
    
    def _background_flush(self) -> None:
        """Background thread to flush logs periodically."""
        while True:
            try:
                time.sleep(self.flush_interval)
                current_time = time.time()
                
                # Flush if enough time has passed
                if current_time - self.last_flush >= self.flush_interval:
                    self._flush_batch()
                    self.last_flush = current_time
                    
            except Exception as e:
                logging.error(f"Error in background flush: {str(e)}")
    
    def flush(self) -> None:
        """Manually flush all pending logs."""
        self._flush_batch()
    
    def close(self) -> None:
        """Close the logger and flush any remaining logs."""
        self.flush()


# Global BigQuery logger instance
_bigquery_logger: Optional[BigQueryLogger] = None


def get_bigquery_logger() -> Optional[BigQueryLogger]:
    """Get the global BigQuery logger instance."""
    return _bigquery_logger


def init_bigquery_logger(
    project_id: str = "picante-440315",
    dataset_id: str = "picante",
    table_id: str = "logs",
    credentials_path: Optional[str] = None,
    batch_size: int = 100,
    flush_interval: int = 30
) -> BigQueryLogger:
    """Initialize the global BigQuery logger.
    
    Args:
        project_id: Google Cloud project ID
        dataset_id: BigQuery dataset ID
        table_id: BigQuery table ID
        credentials_path: Path to service account JSON file
        batch_size: Number of logs to batch before sending
        flush_interval: Seconds to wait before flushing batch
        
    Returns:
        Initialized BigQuery logger instance
    """
    global _bigquery_logger
    
    _bigquery_logger = BigQueryLogger(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        credentials_path=credentials_path,
        batch_size=batch_size,
        flush_interval=flush_interval
    )
    
    return _bigquery_logger 