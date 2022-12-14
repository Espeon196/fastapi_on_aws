import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import ExceptionMiddleware

from mangum import Mangum
from . import dynamo, models
from .utils import logger, tracer, metrics, MetricUnit
from .router import LoggerRouteHandler

app = FastAPI(root_path=f"/{os.environ['GATEWAY_STAGE_NAME']}/")
app.router.route_class = LoggerRouteHandler
app.add_middleware(ExceptionMiddleware, handlers=app.exception_handlers)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    # Get correlation id from X-Correlation-ID header
    corr_id = request.headers.get("x-correlation-id")
    if not corr_id:
        corr_id = request.scope["aws.context"].aws_request_id

    # Add correlation id to logs
    logger.set_correlation_id(corr_id)

    # Add correlation id to traces
    tracer.put_annotation(key="correlation_id", value=corr_id)

    response = await call_next(request)

    response.headers["X-Correlation-Id"] = corr_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, err):
    metrics.add_metric(name="UnhandledExceptions", unit=MetricUnit.Count, value=1)
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/test")
def get_root():
    return {"message": "Hello world"}


@app.get("/pets", response_model=models.PetListResponse)
def list_pets(next_token: Optional[str] = None):
    return dynamo.list_pets(next_token)


@app.get("/pets/{pet_id}", response_model=models.PetResponse)
def get_pet(pet_id: str):
    try:
        return dynamo.get_pet(pet_id)
    except dynamo.PetNotFoundError:
        raise HTTPException(status_code=404, detail="Pet not found")


@app.post("/pets", status_code=201, response_model=models.PetResponse)
def post_pet(payload: models.CreatePayload):
    res = dynamo.create_pet(kind=payload.kind, name=payload.name)
    metrics.add_metric(name="CreatePets", unit=MetricUnit.Count, value=1)
    return res


@app.patch("/pets/{pet_id}", status_code=204)
def update_pet(pet_id: str, payload: models.UpdatePayload):
    try:
        return dynamo.update_pet(
            pet_id=pet_id,
            kind=payload.kind,
            name=payload.name,
        )
    except dynamo.PetNotFoundError:
        raise HTTPException(status_code=404, detail="Pet not found")


@app.delete("/pets/{pet_id}", status_code=204)
def delete_pet(pet_id: str):
    try:
        dynamo.delete_pet(pet_id)
    except dynamo.PetNotFoundError:
        raise HTTPException(status_code=404, detail="Pet not found")


@app.get("/fail")
def fail():
    some_dict = {}
    return some_dict["missing_key"]


handler = Mangum(app)

# Add tracing
handler.__name__ = "handler"
handler = tracer.capture_lambda_handler(handler)
# Add logging
handler = logger.inject_lambda_context(handler, clear_state=True)
# Add metrics last to properly flush metrics
handler = metrics.log_metrics(handler, capture_cold_start_metric=True)
