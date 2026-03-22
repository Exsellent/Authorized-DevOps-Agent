from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Allowed origins for the frontend UI.
# In production, replace ["*"] with specific origins like ["http://localhost:8000"].
CORS_ORIGINS = ["*"]


def setup_cors(app: FastAPI):
    """
    Enable CORS for browser access from the frontend UI.

    This is the single source of truth for CORS configuration.
    All agents use this via MCPAgent base class.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
