from agents.progress.agent import ProgressAgent

agent = ProgressAgent()
app = agent.app


@app.get("/")
async def root():
    return {
        "message": "Progress Agent is running!",
        "agent": agent.name,
        "available_tools": list(agent.tools.keys()),
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(agent.app, host="0.0.0.0", port=8602)