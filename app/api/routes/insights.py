from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.services.graphify_service import run_graphify, parse_graphify_output
from app.services.insights_service import InsightsService

router = APIRouter(prefix="/api/insights", tags=["insights"])
insights_service = InsightsService()

@router.get("/")
async def get_insights():
    """Get AI insights from existing graphify output"""
    try:
        graph_data = parse_graphify_output()
        insights = insights_service.generate_all(graph_data)
        return {"status": "ok", "data": insights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/refresh")
async def refresh_insights(background_tasks: BackgroundTasks, path: str = "."):
    """Re-run graphifyy and regenerate insights"""
    try:
        graph_data = await run_graphify(path)
        insights = insights_service.generate_all(graph_data)
        return {"status": "refreshed", "data": insights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def get_health_score():
    """Just the health score"""
    try:
        graph_data = parse_graphify_output()
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])
        return insights_service._health_score(nodes, edges)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))