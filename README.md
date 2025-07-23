# Thinkion Report Downloader API

Efficient API for transforming Thinkion logs into JSON format for N8N integration, deployed on Railway.

## 📁 Project Structure

```
├── app/                    # Main application code
│   ├── main.py            # FastAPI application
│   ├── thinkion_downloader.py # Report downloader implementation
│   ├── gs_uploader.py     # Google Sheets integration
│   └── __init__.py        # Package initialization
├── data/                  # Data storage
│   ├── logs/              # Application logs
│   └── downloads/         # Downloaded files
├── tests/                 # Test files (currently empty)
├── requirements.txt       # Python dependencies
├── Dockerfile            # Container configuration
└── README.md             # This file
```

## 🚀 Quick Start

### For N8N Integration

Use HTTP Request node with:
- **Method**: GET/POST
- **URL**: `https://your-railway-url.up.railway.app/endpoint`
- **Headers**: `Content-Type: application/json`

### Main Endpoints

- `GET /health` - Health check
- `POST /download` - Download reports
- `GET /jobs/{job_id}` - Check job status
- `GET /logs/{job_id}` - Get job logs

### Railway Deployment

1. Push to GitHub
2. Connect repository to Railway
3. Railway auto-deploys using the Dockerfile

## 🔧 Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run API locally
uvicorn app.main:app --reload

# Run tests (when implemented)
pytest tests/
``` 