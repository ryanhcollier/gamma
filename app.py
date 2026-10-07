"""
FastAPI application for Dealer Gamma positioning and 60-day historical ledger.
"""

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os

from gamma_engine import engine

root_path = os.getenv("ROOT_PATH", "")
app = FastAPI(
    title="Dealer Gamma Positioning System",
    description="SPY/SPX Dealer Gamma Exposure, Zero-Gamma Flip, and 60-Day Historical Backtest Engine",
    version="1.0.0",
    root_path=root_path,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/gamma/static", StaticFiles(directory=STATIC_DIR), name="gamma_static")


@app.get("/api/action")
@app.get("/gamma/api/action")
async def get_action(
    symbol: str = Query("SPY", description="Ticker symbol (SPY or SPX)"),
    refresh: bool = Query(False, description="Force live data refresh"),
):
    """
    Returns current day's live dealer gamma signal, spot price, flip strike,
    call wall, put wall, regime, and 1-to-2 sentence cash-account action text.
    """
    return engine.compute_live_action(symbol=symbol, force_refresh=refresh)


@app.get("/api/history")
@app.get("/gamma/api/history")
async def get_history(
    symbol: str = Query("SPY", description="Ticker symbol (SPY or SPX)"),
    refresh: bool = Query(False, description="Force live data refresh"),
):
    """
    Returns 60 completed trading sessions with pre-market gamma levels,
    day outcomes (WIN / LOSS / SCRATCH), and win/loss summary statistics.
    """
    return engine.compute_60_day_history(symbol=symbol, force_refresh=refresh)


@app.get("/health")
@app.get("/gamma/health")
async def health_check():
    """Health check endpoint for Hostinger deployment monitoring."""
    return {"status": "ok", "service": "dealer_gamma_engine", "subpath": "/gamma"}


@app.get("/", response_class=HTMLResponse)
@app.get("/gamma", response_class=HTMLResponse)
@app.get("/gamma/", response_class=HTMLResponse)
async def serve_index():
    """Serves the minimalist, brutalist single-page application."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>Dealer Gamma Engine is running</h1>", status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
