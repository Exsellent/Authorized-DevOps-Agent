from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def root():
    return {"service": "Authorized DevOps Agent", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "authorized_devops_agent"}
