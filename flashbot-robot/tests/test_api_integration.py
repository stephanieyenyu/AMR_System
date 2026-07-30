import pytest

flask = pytest.importorskip("flask", reason="Flask is required for API integration tests")
pytest.importorskip("flask_sqlalchemy", reason="Flask-SQLAlchemy is required for API integration tests")
Flask = flask.Flask

from aurobox.api import api_bp
from aurobox.models import Door, DoorStatus, RobotState, db


class DummyClient:
    def __init__(self):
        self.completed_tasks = []
        self.cancelled_tasks = []

    def custom_complete(self, payload):
        self.completed_tasks.append(payload)
        return {"message": "SUCCESS"}

    def custom_call_cancel(self, payload):
        self.cancelled_tasks.append(payload)
        return {"message": "SUCCESS"}


class DummyController:
    def __init__(self):
        self.client = DummyClient()
        self.custom_calls = []
        self.door_calls = []
        self.cancel_calls = []
        self.custom_content_calls = []

    def wait_until_arrived(self, sn, timeout_seconds=300, poll_interval=5):
        return True

    def custom_call2(self, payload):
        self.custom_calls.append(payload)
        return {"message": "SUCCESS", "data": {"task_id": "TASK-001"}}

    def control_doors(self, sn, control_states):
        self.door_calls.append({"sn": sn, "control_states": control_states})
        return {"message": "SUCCESS"}

    def get_status_summary(self, sn):
        return {"move_state": "IDLE", "sn": sn}

    def custom_complete(self, payload):
        return self.client.custom_complete(payload)

    def custom_call_cancel(self, payload):
        self.cancel_calls.append(payload)
        return self.client.custom_call_cancel(payload)

    def custom_content(self, payload=None, **kwargs):
        call_payload = payload if payload is not None else kwargs
        self.custom_content_calls.append(call_payload)
        return {"message": "SUCCESS"}


class DummyThread:
    created = []

    def __init__(self, target=None, args=None, daemon=None, kwargs=None):
        self.target = target
        self.args = args or ()
        self.daemon = daemon
        self.kwargs = kwargs or {}
        self.started = False
        DummyThread.created.append(self)

    def start(self):
        # Keep integration tests deterministic; avoid real background loops.
        self.started = True


@pytest.fixture
def api_ctx(monkeypatch, tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'test_api.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["ROBOT_SN"] = "TEST_SN_001"
    app.config["SHOP_ID"] = "SHOP-001"
    app.config["DEFAULT_MAP_NAME"] = "MAP-A"
    app.config["DOOR_MODE"] = "4_DOORS"
    app.config["HOME_POINT_NAME"] = "管理室"
    app.config["CENTRAL_API_BASE_URL"] = "https://central.example.com"

    db.init_app(app)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        db.create_all()
        for door_number in ("H_01", "H_02", "H_03", "H_04"):
            db.session.add(
                Door(
                    sn=app.config["ROBOT_SN"],
                    door_number=door_number,
                    status=DoorStatus.EMPTY.value,
                )
            )
        db.session.commit()

    controller = DummyController()
    app.pudu_controller = controller
    app.home_point = app.config["HOME_POINT_NAME"]
    app.charge_point = "閃閃充電"
    DummyThread.created = []

    monkeypatch.setattr("aurobox.api.threading.Thread", DummyThread)
    monkeypatch.setattr("aurobox.api.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("aurobox.services.time.sleep", lambda _seconds: None, raising=False)
    monkeypatch.setattr("aurobox.api.check_and_return_home_if_empty", lambda: False)

    return {
        "app": app,
        "client": app.test_client(),
        "controller": controller,
    }


def _get_door(app, package_id):
    with app.app_context():
        return Door.query.filter_by(sn=app.config["ROBOT_SN"], package_id=package_id).order_by(Door.door_number).first()


def _get_doors(app, package_id):
    with app.app_context():
        return Door.query.filter_by(sn=app.config["ROBOT_SN"], package_id=package_id).order_by(Door.door_number).all()


def test_assign_and_load_flow(api_ctx):
    client = api_ctx["client"]
    app = api_ctx["app"]

    assign_res = client.post("/api/packages/PKG-A/assign", json={"quantity": 2})
    assert assign_res.status_code == 200
    assert assign_res.json["status"] == "success"
    assert len(assign_res.json["door_numbers"]) == 2

    doors = _get_doors(app, "PKG-A")
    assert len(doors) == 2
    assert all(d.status == DoorStatus.ASSIGNED.value for d in doors)
    assert any(thread.started for thread in DummyThread.created)

    load_res = client.post("/api/doors/load")
    assert load_res.status_code == 200

    doors = _get_doors(app, "PKG-A")
    assert all(d.status == DoorStatus.FULL.value for d in doors)


def test_dispatch_writes_task_id(api_ctx):
    client = api_ctx["client"]
    app = api_ctx["app"]

    client.post("/api/packages/PKG-B/assign", json={"quantity": 1})
    client.post("/api/doors/load")

    dispatch_res = client.post(
        "/api/robot/dispatch",
        json={"point": "Unit-1201", "package_id": "PKG-B"},
    )

    assert dispatch_res.status_code == 200
    assert dispatch_res.json["task_id"] == "TASK-001"
    assert dispatch_res.json["polling"] is True

    with app.app_context():
        robot_state = RobotState.query.filter_by(sn=app.config["ROBOT_SN"]).first()
        assert robot_state is not None
        assert robot_state.current_task_id == "TASK-001"
        assert robot_state.last_point == "Unit-1201"


def test_complete_flow_releases_door(api_ctx):
    client = api_ctx["client"]
    app = api_ctx["app"]

    with app.app_context():
        doors = Door.query.filter_by(sn=app.config["ROBOT_SN"]).order_by(Door.door_number).limit(2).all()
        for door in doors:
            door.package_id = "PKG-C"
            door.status = DoorStatus.FULL.value
        db.session.commit()

    complete_res = client.post("/api/packages/PKG-C/complete")
    assert complete_res.status_code == 200
    assert complete_res.json["status"] == "success"

    with app.app_context():
        doors = Door.query.filter_by(sn=app.config["ROBOT_SN"]).order_by(Door.door_number).limit(2).all()
        assert all(d.status == DoorStatus.EMPTY.value for d in doors)
        assert all(d.package_id is None for d in doors)


def test_cancel_flow_keeps_full_and_clears_task(api_ctx):
    client = api_ctx["client"]
    app = api_ctx["app"]

    with app.app_context():
        door = Door.query.filter_by(sn=app.config["ROBOT_SN"], door_number="H_02").first()
        door.package_id = "PKG-D"
        door.status = DoorStatus.FULL.value
        db.session.add(RobotState(sn=app.config["ROBOT_SN"], last_point="Unit-801", current_task_id="TASK-D"))
        db.session.commit()

    cancel_res = client.post("/api/packages/PKG-D/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json["status"] == "success"

    with app.app_context():
        door = Door.query.filter_by(sn=app.config["ROBOT_SN"], door_number="H_02").first()
        assert door.status == DoorStatus.FULL.value
        assert door.package_id == "PKG-D"
        robot_state = RobotState.query.filter_by(sn=app.config["ROBOT_SN"]).first()
        assert robot_state.current_task_id is None
