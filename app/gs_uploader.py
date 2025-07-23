import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import os

def read_google_sheet_service_account(json_keyfile_path, spreadsheet_id, sheet_name=None):
    """
    Read Google Sheets using service account credentials
    
    Args:
        json_keyfile_path (str): Path to your service account JSON key file
        spreadsheet_id (str): The ID of your Google Spreadsheet
        sheet_name (str, optional): Name of the specific sheet to read
    
    Returns:
        list: The spreadsheet data as a list of dictionaries
    """
    # Define the scope
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    
    # Load credentials
    credentials = Credentials.from_service_account_file(json_keyfile_path, scopes=scope)
    
    # Authorize the client
    gc = gspread.authorize(credentials)
    
    # Open the spreadsheet
    spreadsheet = gc.open_by_key(spreadsheet_id)
    
    # Get the specific sheet or the first sheet
    if sheet_name:
        sheet = spreadsheet.worksheet(sheet_name)
    else:
        sheet = spreadsheet.sheet1
    
    # Get all values
    data = sheet.get_all_records()
    
    return data