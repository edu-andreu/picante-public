"""
Thinkion Report Downloader

A script to automate downloading reports from Thinkion POS system.
Handles login, report downloads, and file management with proper error handling.
"""

#############################################################################
# Imports
#############################################################################

# Standard library imports
import argparse
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any

# Third-party imports
import psutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException
)

# Local imports
from .gs_uploader import read_google_sheet_service_account
from .bigquery_logger import get_bigquery_logger, init_bigquery_logger

#############################################################################
# Configuration
#############################################################################

@dataclass
class Config:
    """Configuration settings for the downloader."""
    # Base paths
    BASE_DIR: Path = Path("/app")  # Railway deployment path
    DOWNLOADS_DIR: Path = BASE_DIR / "data" / "downloads"
    LOGS_DIR: Path = BASE_DIR / "data" / "logs"
    
    # Chrome driver path - auto-detect based on environment
    def _get_chrome_driver_path(self) -> str:
        """Get Chrome driver path based on environment."""
        import os
        import shutil
        
        # For Railway/production environments, use system chromedriver
        if os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('PORT'):
            # Try to find chromedriver in system PATH
            chrome_path = shutil.which('chromedriver')
            if chrome_path:
                return chrome_path
            # Fallback paths for common installations
            for path in ['/usr/bin/chromedriver', '/usr/local/bin/chromedriver']:
                if os.path.exists(path):
                    return path
        
        # For local development, use the local chromedriver
        local_driver = self.BASE_DIR / "chromedriver-mac-arm64" / "chromedriver"
        if local_driver.exists():
            return str(local_driver)
        
        # Final fallback
        return "chromedriver"  # Assume it's in PATH
    
    CHROME_DRIVER_PATH: str = None  # Will be set by property
    
    def __post_init__(self):
        """Set Chrome driver path after initialization."""
        if self.CHROME_DRIVER_PATH is None:
            self.CHROME_DRIVER_PATH = self._get_chrome_driver_path()
        
        # Create base directories
        self.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Timeouts and delays
    WAIT_SECONDS: int = 30  # 30 seconds per report
    DAYS_FROM_TODAY: int = 1
    HUMAN_DELAY: Dict[str, float] = field(default_factory=lambda: {
        "min_seconds": 1.0,
        "max_seconds": 3.0
    })
    
    # Reports settings
    GS_CREDENTIALS_DIR: Path = BASE_DIR / "picante-440315-972b3ea2fd18.json"
    spreadsheet_id: str = "1IrCEdyi1yrYFnkCSuvURXtTR3jPAcfhjrjYWnS8IFrA"
    sheet_name: str = "reports_settings"
    
    def _load_reports_from_google_sheets(self) -> List[Dict]:
        """Load reports data from Google Sheets.
        
        Returns:
            List[Dict]: List of report configurations from Google Sheets
        """
        # Get the current directory and construct the path to the JSON file
        current_dir = Path(__file__).parent.parent
        json_keyfile_path = current_dir / "picante-440315-972b3ea2fd18.json"
        
        # Load data from Google Sheets
        data = read_google_sheet_service_account(
            str(json_keyfile_path),
            self.spreadsheet_id,
            self.sheet_name
        )
        
        # Convert DataFrame to list of dictionaries
        reports_data = []
        for index, row in enumerate(data):
            report = {
                "row_number": index + 2,  # Google Sheets rows start at 1, but we skip header
                "Reporte_Type": row.get("Report_Type", ""),
                "Report_Id": str(row.get("Report_Id", "")),
                "Report_Name": row.get("Report_Name", ""),
                "Report_Url_Param": row.get("Report_Url_Param", "")
            }
            reports_data.append(report)
        
        return reports_data
    
    @property
    def REPORTS_DATA(self) -> List[Dict]:
        """Get reports data from Google Sheets or fallback data.
        
        Returns:
            List[Dict]: List of report configurations
        """
        return self._load_reports_from_google_sheets()

# Create a global config instance
config = Config()

#############################################################################
# Web Element Selectors
#############################################################################

@dataclass
class Selectors:
    """Web element selectors for different pages."""
    
    class Login:
        """Login page selectors."""
        EMAIL = (By.ID, "email")
        PASSWORD = (By.ID, "pass")
        SUBMIT = (By.CSS_SELECTOR, "button[type='submit']")
        ERROR = (By.CSS_SELECTOR, "article.error")
    
    class DateStore:
        """Date and store filter selectors."""
        IFRAME_LINK = (By.CSS_SELECTOR, "a[href='#iframe_modal_external']")
        IFRAME = (By.CSS_SELECTOR, "iframe[src='standalone/filter_dating.html']")
        DATES_TAB = (By.XPATH, "//a[@data-tab='tab_dates']")
        DAYS_INPUT = (By.ID, "q_days_input")
        STORES_TAB = (By.XPATH, "//a[@data-tab='tab_establishments']")
        SELECT_ALL = (By.XPATH, "//li[@onclick='select_all_establishments()']")
        APPLY = (By.CSS_SELECTOR, "section.buttons a[onclick='save()']")
    
    class Download:
        """Download page selectors."""
        EMPTY_GRID = (By.ID, "grid_empty")
        ERROR_GRID = (By.ID, "grid_error")
        DOWNLOAD_BUTTON = (By.XPATH, "//button[.//span[text()='Exportar']]")
        LOADING = (By.CSS_SELECTOR, ".loading")
    
    class Logout:
        """Logout page selectors."""
        NAVBAR_BUTTON = (By.ID, "navbtn_menu_primary")
        LOGOUT_BUTTON = (By.LINK_TEXT, "Cerrar Sesión")
        OVERLAY = (By.CSS_SELECTOR, "div.valign-wrapper")

#############################################################################
# Logging
#############################################################################

class JobLogger:
    """Custom logger for handling job and task IDs with BigQuery integration."""
    
    def __init__(self, job_id: Optional[str] = None, account_id: Optional[str] = None):
        """Initialize the logger.
        
        Args:
            job_id: Optional job ID. If not provided, a new one will be generated.
            account_id: Optional account ID for BigQuery logging.
        """
        self.job_id = job_id or str(uuid.uuid4())
        self.account_id = account_id
        self.task_counter = 0
        
        # Create logs directory if it doesn't exist
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Set up job-specific log file
        self.log_file = config.LOGS_DIR / f"{self.job_id}.log"
        
        # Clean log file before setting up logger
        try:
            with open(self.log_file, 'w') as f:
                f.write('')
        except Exception as e:
            print(f"Error: Failed to clean log file: {str(e)}")
        
        # Set up logger
        self.logger = logging.getLogger(f'thinkion_downloader_{self.job_id}')
        self.logger.setLevel(logging.DEBUG)
        
        # Remove any existing handlers
        self.logger.handlers = []
        
        # Create file handler
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.DEBUG)
        
        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - [job_id=%(job_id)s task_id=%(task_id)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        
        # Add handler to logger
        self.logger.addHandler(file_handler)
        self.logger.propagate = False
        
        # Get BigQuery logger
        self.bigquery_logger = get_bigquery_logger()
        
        # Log the start of a new job
        self.info(f"Starting new job with ID: {self.job_id}")
    
    def _get_task_id(self) -> str:
        """Get the next task ID."""
        self.task_counter += 1
        return f"{self.job_id}_{self.task_counter}"
    
    def _log(self, level: int, msg: str, report_name: Optional[str] = None, error_details: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log a message with the current job and task IDs."""
        task_id = self._get_task_id()
        
        # Log to file
        extra = {
            'job_id': self.job_id,
            'task_id': task_id
        }
        self.logger.log(level, msg, extra=extra)
        
        # Log to BigQuery if available
        if self.bigquery_logger:
            try:
                level_name = logging.getLevelName(level)
                self.bigquery_logger.log(
                    job_id=self.job_id,
                    level=level_name,
                    message=msg,
                    task_id=task_id,
                    account_id=self.account_id,
                    report_name=report_name,
                    error_details=error_details,
                    metadata=metadata
                )
            except Exception as e:
                # Don't let BigQuery errors break the application
                print(f"BigQuery logging error: {str(e)}")
    
    def debug(self, msg: str, report_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log a debug message."""
        self._log(logging.DEBUG, msg, report_name=report_name, metadata=metadata)
    
    def info(self, msg: str, report_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log an info message."""
        self._log(logging.INFO, msg, report_name=report_name, metadata=metadata)
    
    def warning(self, msg: str, report_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log a warning message."""
        self._log(logging.WARNING, msg, report_name=report_name, metadata=metadata)
    
    def error(self, msg: str, report_name: Optional[str] = None, error_details: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log an error message."""
        self._log(logging.ERROR, msg, report_name=report_name, error_details=error_details, metadata=metadata)
    
    def critical(self, msg: str, report_name: Optional[str] = None, error_details: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Log a critical message."""
        self._log(logging.CRITICAL, msg, report_name=report_name, error_details=error_details, metadata=metadata)

#############################################################################
# Main Class
#############################################################################

class ThinkionReportDownloader:
    """Main class for downloading reports from Thinkion."""
    
    def __init__(
        self,
        account_data: List[Dict],
        reports_data: List[Dict],
        job_id: Optional[str] = None,
        wait_seconds: int = config.WAIT_SECONDS * len(config.REPORTS_DATA)
    ) -> None:
        """Initialize the downloader.
        
        Args:
            account_data: List of account credentials and settings
            reports_data: List of reports to download
            job_id: Optional job ID. If not provided, a new one will be generated.
            wait_seconds: Maximum wait time for operations
        """
        self.job_id = job_id or str(uuid.uuid4())
        self.account_data = account_data
        self.reports_data = reports_data
        self.wait_seconds = wait_seconds
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self.account_id: Optional[str] = None
        
        # Set up job-specific directories
        self.download_dir = config.DOWNLOADS_DIR / self.job_id
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
        # Set up job-specific logger with account_id
        account_id = str(account_data[0]["Account_Id"]) if account_data else None
        self.logger = JobLogger(self.job_id, account_id=account_id)
        
        # Configure Chrome options
        self.chrome_options = Options()
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--window-size=1920,1080')
        
        # Set download preferences
        prefs = {
            "download.default_directory": str(self.download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True
        }
        self.chrome_options.add_experimental_option("prefs", prefs)
        
        self.logger.debug("Success: Downloader initialized")

    def _human_like_delay(self) -> None:
        """Add a random delay to simulate human behavior."""
        delay = random.uniform(
            config.HUMAN_DELAY["min_seconds"],
            config.HUMAN_DELAY["max_seconds"]
        )
        time.sleep(delay)

    def _kill_chrome_drivers(self) -> None:
        """Kill any existing Chrome driver processes."""
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if 'chromedriver' in proc.info['name'].lower():
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        self.logger.debug("Success: Killed existing Chrome drivers")

    def cleanup(self, kill_drivers: bool = True) -> None:
        """Clean up browser resources.
        
        Args:
            kill_drivers: Whether to kill Chrome driver processes
        """
        if self.driver:
            try:
                self.driver.quit()
                self.logger.debug("Success: Close browser")
            except Exception as e:
                self.logger.error(f"Error: Close browser: {str(e)}")
            finally:
                self.driver = None
        
        if kill_drivers:
            self._kill_chrome_drivers()

    def setup_driver(self) -> None:
        """Initialize and configure the Chrome driver."""
        self._kill_chrome_drivers()
        
        try:
            self.driver = webdriver.Chrome(
                service=Service(config.CHROME_DRIVER_PATH),
                options=self.chrome_options
            )
            self.wait = WebDriverWait(self.driver, self.wait_seconds)
            self.logger.debug("Success: Initialize Chrome driver")
        except Exception as e:
            self.logger.error(f"Error: Initialize Chrome driver: {str(e)}")
            raise

    def login(self, account: Dict) -> None:
        """Handle login process.
        
        Args:
            account: Account credentials dictionary
        
        Raises:
            Exception: If login fails
        """
        try:
            self.driver.get(account["Store_POS_Url"])
            self._human_like_delay()
            
            self.driver.find_element(*Selectors.Login.EMAIL).send_keys(account["Store_POS_Username"])
            self.driver.find_element(*Selectors.Login.PASSWORD).send_keys(account["Store_POS_Pass"])
            self.driver.find_element(*Selectors.Login.SUBMIT).click()
            self._human_like_delay()
            
            try:
                error_element = self.driver.find_element(*Selectors.Login.ERROR)
                error_msg = f"Error: Login to account {account['Account_Id']}: {error_element.text}"
                self.logger.error(error_msg)
                raise Exception(error_msg)
            except NoSuchElementException:
                self.logger.debug(f"Success: Login to account {account['Account_Id']}")
        except Exception as e:
            self.logger.error(f"Error: Login to account {account['Account_Id']}: {str(e)}")
            raise

    def _handle_iframe(self) -> None:
        """Handle iframe switching for date and store selection."""
        try:
            iframe_link = self.driver.find_element(*Selectors.DateStore.IFRAME_LINK)
            iframe_link.click()
            
            iframe = self.driver.find_element(*Selectors.DateStore.IFRAME)
            self.driver.switch_to.frame(iframe)
            self.logger.debug("Success: Click filters button")
        except Exception as e:
            self.logger.error(f"Error: Click filters button: {str(e)}")
            raise

    def _set_dates(self) -> None:
        """Set date range for reports."""
        try:
            dates_tab = self.driver.find_element(*Selectors.DateStore.DATES_TAB)
            dates_tab.click()
            
            days_input = self.driver.find_element(*Selectors.DateStore.DAYS_INPUT)
            self.driver.execute_script("arguments[0].value = '';", days_input)
            days_input.send_keys(str(config.DAYS_FROM_TODAY))
            days_input.send_keys(Keys.ENTER)
            self._human_like_delay()
            self.logger.debug(f"Success: Set date")
        except Exception as e:
            self.logger.error(f"Error: Set date: {str(e)}")
            raise

    def _set_stores(self, web_group_selector: str) -> None:
        """Set store selection.
        
        Args:
            web_group_selector: CSS selector for store group
        """
        try:
            stores_tab = self.driver.find_element(*Selectors.DateStore.STORES_TAB)
            stores_tab.click()
            self._human_like_delay()

            select_all_checkbox = self.driver.find_element(By.CSS_SELECTOR, web_group_selector)
            if not select_all_checkbox.is_selected():
                select_all_stores = self.driver.find_element(By.XPATH, "//li[@onclick='select_all_establishments()']")
                select_all_stores.click()
                self._human_like_delay()
            self.logger.debug("Success: Set group")
        except Exception as e:
            self.logger.error(f"Error: Set group: {str(e)}")
            raise

    def set_date_store(self, web_group_selector: str) -> None:
        """Configure date and store filters.
        
        Args:
            web_group_selector: CSS selector for store group
        """
        try:
            self._handle_iframe()
            self._set_dates()
            self._set_stores(web_group_selector)
            
            # Apply changes
            apply_button = self.driver.find_element(*Selectors.DateStore.APPLY)
            apply_button.click()
            self._human_like_delay()
            self.logger.debug(f"Success: Apply filters")
        except Exception as e:
            self.logger.error(f"Error: Apply filters: {str(e)}")
            raise

    def close_session(self) -> None:
        """Handle logout process."""
        try:
            dashboard_url = self.driver.current_url.replace("login", "dashboards")
            self.driver.get(dashboard_url)
            self._human_like_delay()

            try:
                overlay = self.driver.find_element(*Selectors.Logout.OVERLAY)
                WebDriverWait(self.driver, 5).until(EC.invisibility_of_element(overlay))
            except:
                pass

            menu_button = self.driver.find_element(*Selectors.Logout.NAVBAR_BUTTON)
            menu_button.click()
            self._human_like_delay()
            
            logout_link = self.driver.find_element(*Selectors.Logout.LOGOUT_BUTTON)
            logout_link.click()
            self.logger.debug(f"Success: Logout from account {self.account_id}")
        except Exception as e:
            self.logger.error(f"Error: Logout from account {self.account_id}: {str(e)}")

    def _check_element_display(self, selector: Tuple[str, str]) -> bool:
        """Check if an element is displayed.
        
        Args:
            selector: Element selector tuple
            
        Returns:
            bool: True if element is displayed, False otherwise
        """
        try:
            element = self.driver.find_element(*selector)
            style = element.get_attribute("style")
            return "display: block" in style
        except (NoSuchElementException, Exception):
            return False

    def check_invalid_report_url(self) -> bool:
        """Check if the report URL is invalid."""
        return self._check_element_display(Selectors.Download.ERROR_GRID)

    def check_no_data_message(self) -> bool:
        """Check if there is a no data message displayed."""
        return self._check_element_display(Selectors.Download.EMPTY_GRID)

    def _wait_for_download_completion(self, file_extension: str = '.xls') -> Optional[str]:
        """Wait for a file to be completely downloaded.
        
        Args:
            file_extension: Expected file extension
            
        Returns:
            Optional[str]: Path to downloaded file if successful, None otherwise
        """
        start_time = time.time()
        last_size = -1
        downloaded_file = None

        while time.time() - start_time < self.wait_seconds:
            try:
                files = [
                    f for f in os.listdir(self.download_dir)
                    if f.endswith(file_extension)
                ]
                if files:
                    current_file = max(
                        [os.path.join(self.download_dir, f) for f in files],
                        key=os.path.getmtime
                    )

                    current_size = os.path.getsize(current_file)
                    
                    if current_size == last_size and current_size > 0:
                        downloaded_file = current_file
                        break

                    last_size = current_size

            except (OSError, IOError) as e:
                self.logger.error(f"Error: Check download status: {str(e)}")
                return None
                
            time.sleep(0.5)  # Increased sleep time to reduce CPU usage
            
        if not downloaded_file:
            self.logger.error("Error: Download timeout - no file found or file size not stable")
            
        return downloaded_file

    def _click_download_button(self) -> bool:
        """Click the download button and wait for download to start.
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Wait for any loading indicators to disappear
            try:
                loading = self.driver.find_element(*Selectors.Download.LOADING)
                WebDriverWait(self.driver, 5).until(EC.invisibility_of_element(loading))
            except (NoSuchElementException, TimeoutException):
                pass  # No loading indicator found or already gone

            # Wait for the download button to be clickable
            download_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(Selectors.Download.DOWNLOAD_BUTTON)
            )

            # Try to scroll the button into view
            self.driver.execute_script("arguments[0].scrollIntoView(true);", download_button)
            self._human_like_delay()

            # Try to click using JavaScript if regular click fails
            try:
                download_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", download_button)

            self.logger.debug("Success: Click download button")
            return True
        except TimeoutException:
            self.logger.error("Error: Download button not found")
            return False
        except Exception as e:
            self.logger.error(f"Error: Click download button: {str(e)}")
            return False

    def rename_downloaded_file(self, target_filename: str, file_extension: str = '.xls') -> bool:
        """Rename the downloaded file with timestamp.
        
        Args:
            target_filename: Base filename
            file_extension: File extension
            
        Returns:
            bool: True if rename successful, False otherwise
        """
        try:
            downloaded_file = self._wait_for_download_completion(file_extension)
            
            if not downloaded_file:
                self.logger.error(f"Error: No file downloaded for {target_filename}")
                return False
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_filename = (
                f"accountID={self.account_id}:"
                f"{os.path.splitext(target_filename)[0]}_{timestamp}{file_extension}"
            )
            new_file = self.download_dir / new_filename
            
            # Ensure the file exists before renaming
            if not os.path.exists(downloaded_file):
                self.logger.error(f"Error: Downloaded file not found: {downloaded_file}")
                return False
                
            os.rename(downloaded_file, new_file)
            self.logger.debug(f"Success: Download report: {target_filename}")
            self.logger.debug(f"Success: Rename report to {new_filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error: Rename report to {new_filename}: {str(e)}")
            return False

    def _build_report_url(self, account: Dict, report: Dict) -> str:
        """Build the report URL for a given account and report.
        
        Args:
            account: Account dictionary containing Store_POS_Url
            report: Report dictionary containing Report_Url_Param
            
        Returns:
            str: Complete report URL
        """
        report_url_base = account["Store_POS_Url"].replace("/login.html", "")
        report_url_param = report["Report_Url_Param"]
        report_url = f"{report_url_base}/{report_url_param}.html"
        return report_url
    
    def _navigate_to_report(self, account: Dict, report: Dict) -> bool:
        """Navigate to the report URL.
        
        Args:
            report_url: URL of the report
            
        Returns:
            bool: True if navigation successful, False otherwise
        """
        try:
            # Build the report URL using the existing function
            report_url = self._build_report_url(account, report)

            # Navigate to the report URL
            self.driver.get(report_url)
            self._human_like_delay()
            self.logger.debug(f"Success: Navigate to report URL: {report_url}")
            return not self.check_invalid_report_url()
        except Exception as e:
            self.logger.error(f"Error: Navigate to report URL: {str(e)}")
            return False

    def _reset_browser_state(self) -> None:
        """Reset browser state and handle any alerts."""
        try:
            self.driver.switch_to.default_content()
            try:
                alert = self.driver.switch_to.alert
                alert.accept()
            except:
                pass
            self._human_like_delay()
        except Exception as e:
            self.logger.error(f"Error: Reset browser state: {str(e)}")

    def save_and_export(self, account: Dict, report: Dict) -> str:
        """Handle report download and export.
        
        Args:
            account: Account dictionary containing Store_POS_Url
            report: Report dictionary containing Report_Url_Param and Report_Name
            
        Returns:
            str: Status of the operation ('success', 'failed', or 'no_data')
        """
        report_name = report.get("Report_Name", "Unknown")
        
        try:
            # Log with report context
            self.logger.info(
                f"Starting export for report: {report_name}",
                report_name=report_name,
                metadata={
                    "report_id": report.get("Report_Id"),
                    "report_type": report.get("Reporte_Type"),
                    "account_id": account.get("Account_Id")
                }
            )
            
            if not self._navigate_to_report(account, report):
                self.logger.error(f"Error: Navigate to report: {report_name}")
                return "failed"

            if self.check_no_data_message():
                self.logger.debug(f"Success: No data available for report: {report_name}")
                return "no_data"

            if not self._click_download_button():
                return "failed"
            
            if not self._wait_for_download_completion('.xls'):
                self.logger.error(f"Error: Download did not complete for report: {report_name}")
                return "failed"
            
            if self.rename_downloaded_file(report['Report_Name'], file_extension='.xls'):
                self.logger.info(
                    f"Successfully exported report: {report_name}",
                    report_name=report_name
                )
                return "success"
            
            return "failed"
            
        except Exception as e:
            error_details = str(e)
            self.logger.error(
                f"Failed to export report: {report_name}",
                report_name=report_name,
                error_details=error_details,
                metadata={
                    "report_id": report.get("Report_Id"),
                    "account_id": account.get("Account_Id")
                }
            )
            raise
        finally:
            self._reset_browser_state()

    def download_all_reports(self, account: Dict) -> None:
        """Download all reports for the current account.
        
        Args:
            account: Account dictionary containing Store_POS_Url
        """
        success_count = 0
        no_data_count = 0
        for report in self.reports_data:
            try:
                result = self.save_and_export(account, report)
                if result == "success":
                    success_count += 1
                    self.logger.info(f"{result.capitalize()}: {report['Report_Name']}")
                elif result == "no_data":
                    no_data_count += 1
            except Exception as e:
                self.logger.error(f"Error: Processing report {report['Report_Name']}: {str(e)}")
                try:
                    self.driver.switch_to.default_content()
                except:
                    pass
                continue
        
        if success_count == len(self.reports_data):
            self.logger.debug("Success: Download all reports")
        else:
            self.logger.debug(f"Info: Downloaded {success_count} out of {len(self.reports_data)} reports")

        if success_count + no_data_count == len(self.reports_data):
            self.logger.info("Success: All reports were scraped")
        else:
            self.logger.error("Error: Failed to scrape all reports")
        
    def process_account(self, account: Dict) -> None:
        """Process a single account.
        
        Args:
            account: Account credentials and settings
        """
        try:
            self.account_id = account['Account_Id']
            self.logger.debug(f"Success: Start processing account {self.account_id}")
            
            self.setup_driver()
            self.login(account)
            self.set_date_store(account["Web_Group_Selector"])
            self.download_all_reports(account)
            self.close_session()
            
            self.logger.debug(f"Success: Finish processing account {self.account_id}")
        except Exception as e:
            self.logger.error(f"Error: Processing account {account['Account_Id']}: {str(e)}")
            raise

    def validate_configuration(self) -> None:
        """Validate the configuration before starting.
        
        Raises:
            ValueError: If configuration is invalid
        """
        if not os.path.exists(config.CHROME_DRIVER_PATH):
            error_msg = f"Chrome driver not found at: {config.CHROME_DRIVER_PATH}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if not self.account_data:
            error_msg = "No account data provided"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        
        for account in self.account_data:
            required_fields = [
                "Account_Id", "Store_POS_Url", "Store_POS_Username",
                "Store_POS_Pass", "Web_Group_Selector"
            ]
            missing_fields = [
                field for field in required_fields
                if field not in account
            ]
            if missing_fields:
                error_msg = (
                    f"Account {account.get('Account_Id', 'Unknown')} "
                    f"missing required fields: {missing_fields}"
                )
                self.logger.error(error_msg)
                raise ValueError(error_msg)

        if not self.reports_data:
            error_msg = "No reports data provided"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        
        for report in self.reports_data:
            required_fields = ["Report_Name", "Report_Url_Param"]
            missing_fields = [
                field for field in required_fields
                if field not in report
            ]
            if missing_fields:
                error_msg = f"Report missing required fields: {missing_fields}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

    def run(self) -> None:
        """Main execution method."""
        try:
            self.logger.info("Success: Start job")
            self.validate_configuration()
            
            for account in self.account_data:
                try:
                    self.process_account(account)
                except Exception as e:
                    if "Login failed" not in str(e):
                        self.logger.error(f"Error: Unexpected error: {str(e)}")
                    continue
            self.cleanup()
        except ValueError as e:
            self.logger.error(f"Error: Configuration error: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Error: Unexpected error: {str(e)}")
            raise
        finally:
            self.logger.info("Success: End job")

#############################################################################
# Main Entry Point
#############################################################################

def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Run Selenium test with POS credentials.'
    )

    parser.add_argument(
        '--account-id',
        type=int,
        required=True,
        help='Account ID to process'
    )
    parser.add_argument(
        '--url',
        type=str,
        required=True,
        help='Store POS URL'
    )
    parser.add_argument(
        '--username',
        type=str,
        required=True,
        help='Store POS username'
    )
    parser.add_argument(
        '--password',
        type=str,
        required=True,
        help='Store POS password'
    )
    parser.add_argument(
        '--selector',
        type=str,
        required=True,
        help='Web group selector for stores'
    )

    args = parser.parse_args()

    if not args.url.startswith(('http://', 'https://')):
        raise ValueError("URL must start with http:// or https://")

    account_data = [{
        "Account_Id": args.account_id,
        "Store_POS_Url": args.url,
        "Store_POS_Username": args.username,
        "Store_POS_Pass": args.password,
        "Web_Group_Selector": args.selector
    }]

    downloader = ThinkionReportDownloader(
        account_data=account_data,
        reports_data=config.REPORTS_DATA,
        job_id=args.account_id,
        wait_seconds=config.WAIT_SECONDS
    )
    
    try:
        downloader.run()
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        exit(1)

if __name__ == "__main__":
    main()


    