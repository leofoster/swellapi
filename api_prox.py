# local_proxy.py
from fastapi import FastAPI, Request
import requests
import uvicorn

app = FastAPI()

@app.get("/marine")
def marine(request: Request):
    resp = requests.get("https://marine-api.open-meteo.com/v1/marine", params=dict(request.query_params), timeout=10)
    return resp.json()

@app.get("/forecast")
def forecast(request: Request):
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=dict(request.query_params), timeout=10)
    return resp.json()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)