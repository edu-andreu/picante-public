import pandas as pd
from bs4 import BeautifulSoup
import os
import requests
from google.cloud import bigquery
from datetime import datetime

def get_latest_job_id():
    # Initialize BigQuery client
    client = bigquery.Client(project="picante-440315")
    
    # Query to get the most recent job_id
    query = """
        SELECT job_id 
        FROM `picante-440315.picante.logs`
        ORDER BY timestamp DESC
        LIMIT 1
    """
    
    query_job = client.query(query)
    results = query_job.result()
    print(results)
    
    # Get the job_id from results
    for row in results:
        return row.job_id
    return None

def get_downloaded_files(job_id):
    # Request the files for the job
    url = f"https://picante-production.up.railway.app/files/{job_id}"
    response = requests.get(url)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Failed to get files. Status code: {response.status_code}")

# Get the latest job_id and its files
latest_job_id = get_latest_job_id()
if latest_job_id:
    downloaded_files = get_downloaded_files(latest_job_id)

""" # Get the root directory (one level up from the script)
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Read the HTML file
with open(os.path.join(root_dir, 'html_excel.txt'), 'r', encoding='utf-8') as file:
    html_content = file.read()

# Parse HTML
soup = BeautifulSoup(html_content, 'html.parser')

# Find the table
table = soup.find('table') """
