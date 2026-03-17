from agents.planner.agent import PlannerAgent

agent = PlannerAgent()
app = agent.app


@app.get("/")
async def root():
    return {
        "message": "Planner Agent is running!",
        "agent": agent.name,
        "available_tools": list(agent.tools.keys()),
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(agent.app, host="0.0.0.0", port=8601)