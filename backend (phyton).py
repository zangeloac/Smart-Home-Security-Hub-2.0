# backend.py
# Requirements: fastapi, uvicorn, sqlalchemy, databases, pydantic, aiosqlite
# Install: pip install fastapi uvicorn sqlalchemy databases aiosqlite

import asyncio
import enum
import json
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sqlalchemy
from databases import Database

DATABASE_URL = "sqlite:///./smart_home.db"

# --- DB setup (SQLAlchemy metadata) ---
metadata = sqlalchemy.MetaData()

devices = sqlalchemy.Table(
    "devices",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("type", sqlalchemy.String, nullable=False),  # e.g., alarm, light, camera
    sqlalchemy.Column("state", sqlalchemy.String, nullable=False), # JSON string of state
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

sensors = sqlalchemy.Table(
    "sensors",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("type", sqlalchemy.String, nullable=False),  # e.g., door, window, motion
    sqlalchemy.Column("is_triggered", sqlalchemy.Integer, nullable=False, default=0),
    sqlalchemy.Column("sensitivity", sqlalchemy.Float, nullable=False, default=1.0),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

events = sqlalchemy.Table(
    "events",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("timestamp", sqlalchemy.DateTime, nullable=False),
    sqlalchemy.Column("level", sqlalchemy.String, nullable=False), # info, warn, critical
    sqlalchemy.Column("source", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("payload", sqlalchemy.String, nullable=True), # JSON serialized
)

engine = sqlalchemy.create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
metadata.create_all(engine)
db = Database(DATABASE_URL)


# --- Domain models / DTOs ---
class DeviceType(str, enum.Enum):
    alarm = "alarm"
    light = "light"
    camera = "camera"
    lock = "lock"


class SensorType(str, enum.Enum):
    door = "door"
    window = "window"
    motion = "motion"
    glass_break = "glass_break"


class DeviceIn(BaseModel):
    name: str
    type: DeviceType
    state: Optional[Dict[str, Any]] = Field(default_factory=dict)


class DeviceOut(DeviceIn):
    id: str
    created_at: datetime
    state: Dict[str, Any]


class SensorIn(BaseModel):
    name: str
    type: SensorType
    sensitivity: float = 1.0


class SensorOut(SensorIn):
    id: str
    is_triggered: bool
    created_at: datetime


class EventOut(BaseModel):
    id: str
    timestamp: datetime
    level: str
    source: str
    payload: Optional[Dict[str, Any]] = None


# --- FastAPI app ---
app = FastAPI(title="Smart Home Security Backend")

# allow your frontend origin (adjust origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to your github pages origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- WebSocket manager for broadcasting events ---
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for conn in list(self.active):
            try:
                await conn.send_text(json.dumps(message, default=str))
            except Exception:
                dead.append(conn)
        for d in dead:
            self.disconnect(d)


manager = ConnectionManager()


# --- Helpers ---
async def log_event(level: str, source: str, payload: Optional[dict] = None):
    ev_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await db.execute(
        events.insert().values(
            id=ev_id,
            timestamp=now,
            level=level,
            source=source,
            payload=json.dumps(payload) if payload is not None else None,
        )
    )
    evt = {
        "id": ev_id,
        "timestamp": now.isoformat(),
        "level": level,
        "source": source,
        "payload": payload,
    }
    # broadcast to websockets
    await manager.broadcast({"type": "event", "event": evt})
    return evt


# --- Startup / shutdown ---
@app.on_event("startup")
async def startup():
    await db.connect()
    # ensure some demo devices/sensors exist
    # add defaults only if empty
    dev_count = await db.fetch_val(sqlalchemy.select([sqlalchemy.func.count()]).select_from(devices))
    sens_count = await db.fetch_val(sqlalchemy.select([sqlalchemy.func.count()]).select_from(sensors))
    if dev_count == 0:
        now = datetime.utcnow()
        await db.execute(devices.insert().values(
            id=str(uuid.uuid4()), name="Main Alarm", type="alarm", state=json.dumps({"armed": False}), created_at=now
        ))
        await db.execute(devices.insert().values(
            id=str(uuid.uuid4()), name="Porch Light", type="light", state=json.dumps({"on": False, "brightness": 80}), created_at=now
        ))
        await db.execute(devices.insert().values(
            id=str(uuid.uuid4()), name="Front Door Lock", type="lock", state=json.dumps({"locked": True}), created_at=now
        ))
    if sens_count == 0:
        now = datetime.utcnow()
        await db.execute(sensors.insert().values(
            id=str(uuid.uuid4()), name="Front Door Sensor", type="door", is_triggered=0, sensitivity=1.0, created_at=now
        ))
        await db.execute(sensors.insert().values(
            id=str(uuid.uuid4()), name="Living Room Motion", type="motion", is_triggered=0, sensitivity=0.9, created_at=now
        ))


@app.on_event("shutdown")
async def shutdown():
    await db.disconnect()


# --- REST endpoints ---

# health
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# list devices
@app.get("/devices", response_model=List[DeviceOut])
async def list_devices():
    rows = await db.fetch_all(sqlalchemy.select([devices]))
    out = []
    for r in rows:
        state = json.loads(r["state"]) if r["state"] else {}
        out.append(DeviceOut(id=r["id"], name=r["name"], type=r["type"], state=state, created_at=r["created_at"]))
    return out


# create device
@app.post("/devices", response_model=DeviceOut, status_code=201)
async def create_device(d: DeviceIn):
    dev_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await db.execute(devices.insert().values(id=dev_id, name=d.name, type=d.type.value, state=json.dumps(d.state), created_at=now))
    return DeviceOut(id=dev_id, name=d.name, type=d.type, state=d.state, created_at=now)


# update device state
@app.patch("/devices/{device_id}", response_model=DeviceOut)
async def update_device_state(device_id: str, payload: dict):
    row = await db.fetch_one(sqlalchemy.select([devices]).where(devices.c.id == device_id))
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")
    state = json.loads(row["state"] or "{}")
    state.update(payload)
    await db.execute(devices.update().where(devices.c.id == device_id).values(state=json.dumps(state)))
    await log_event("info", "device.updated", {"device_id": device_id, "state": state})
    return DeviceOut(id=row["id"], name=row["name"], type=row["type"], state=state, created_at=row["created_at"])


# list sensors
@app.get("/sensors", response_model=List[SensorOut])
async def list_sensors():
    rows = await db.fetch_all(sqlalchemy.select([sensors]))
    out = []
    for r in rows:
        out.append(SensorOut(id=r["id"], name=r["name"], type=r["type"], sensitivity=r["sensitivity"], is_triggered=bool(r["is_triggered"]), created_at=r["created_at"]))
    return out


# manually trigger/untrigger sensor
@app.post("/sensors/{sensor_id}/trigger", response_model=EventOut)
async def trigger_sensor(sensor_id: str, trigger: Optional[dict] = None):
    r = await db.fetch_one(sqlalchemy.select([sensors]).where(sensors.c.id == sensor_id))
    if not r:
        raise HTTPException(status_code=404, detail="Sensor not found")
    # toggle triggered state if body {"value": true/false} else set true
    val = True
    if trigger and "value" in trigger:
        val = bool(trigger["value"])
    await db.execute(sensors.update().where(sensors.c.id == sensor_id).values(is_triggered=int(val)))
    payload = {"sensor_id": sensor_id, "value": val}
    ev = await log_event("warn" if val else "info", "sensor.triggered", payload)
    return JSONResponse(status_code=200, content=ev)


# get recent events
@app.get("/events", response_model=List[EventOut])
async def get_events(limit: int = 50):
    q = sqlalchemy.select([events]).order_by(events.c.timestamp.desc()).limit(limit)
    rows = await db.fetch_all(q)
    out = []
    for r in rows:
        payload = json.loads(r["payload"]) if r["payload"] else None
        out.append(EventOut(id=r["id"], timestamp=r["timestamp"], level=r["level"], source=r["source"], payload=payload))
    return out


# arm/disarm alarm (convenience endpoint) - finds alarm device and toggles "armed" flag
@app.post("/system/arm", response_model=EventOut)
async def arm_system(action: dict):
    # action: {"arm": true/false}
    arm = bool(action.get("arm", True))
    # set all alarm devices' state armed flag
    q = sqlalchemy.select([devices]).where(devices.c.type == "alarm")
    alarm_rows = await db.fetch_all(q)
    for ar in alarm_rows:
        state = json.loads(ar["state"] or "{}")
        state["armed"] = arm
        await db.execute(devices.update().where(devices.c.id == ar["id"]).values(state=json.dumps(state)))
    ev = await log_event("info", "system.armed" if arm else "system.disarmed", {"armed": arm})
    return JSONResponse(status_code=200, content=ev)


# --- WebSocket endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # keep connection alive, optionally receive pings from client
            msg = await websocket.receive_text()
            # simple echo of incoming messages
            await websocket.send_text(json.dumps({"type": "echo", "msg": msg}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# --- Background simulator (demo only) ---
async def sensor_simulator_task(stop_event: asyncio.Event):
    """
    Periodically (randomly) triggers sensors to simulate motion/door events.
    This is for demo/test only — disable in production if you don't want automatic triggers.
    """
    import random
    while not stop_event.is_set():
        # wait a random amount of time
        await asyncio.sleep(random.uniform(3.0, 12.0))
        # pick a sensor
        rows = await db.fetch_all(sqlalchemy.select([sensors]))
        if not rows:
            continue
        s = random.choice(rows)
        # decide whether to trigger (some small probability influenced by sensitivity)
        chance = min(0.9, 0.1 * float(s["sensitivity"]) + 0.05)
        if random.random() < chance:
            # set triggered
            await db.execute(sensors.update().where(sensors.c.id == s["id"]).values(is_triggered=1))
            payload = {"sensor_id": s["id"], "sensor_name": s["name"], "type": s["type"]}
            await log_event("warn", "sensor.simulated_trigger", payload)


# start the simulator when the app runs
_simulator_stop = asyncio.Event()
@app.on_event("startup")
async def start_simulator():
    # spawn background simulator task
    asyncio.create_task(sensor_simulator_task(_simulator_stop))


@app.on_event("shutdown")
async def stop_simulator():
    _simulator_stop.set()


# --- Simple admin endpoint to reset triggers (useful for UI) ---
@app.post("/admin/reset_triggers")
async def reset_triggers():
    await db.execute(sensors.update().values(is_triggered=0))
    ev = await log_event("info", "admin.reset_triggers", {})
    return {"ok": True, "event": ev}
