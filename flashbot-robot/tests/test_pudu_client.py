import pytest

from aurobox.config import load_config, require_config
from aurobox.pudu_client import PuduApiClient
from aurobox.robot import FlashbotController


@pytest.fixture(autouse=True)
def set_required_env(monkeypatch):
    monkeypatch.setenv("Pd_key", "test_app_key")
    monkeypatch.setenv("Pd_secret", "test_app_secret")
    monkeypatch.setenv("Aurotek_id", "test_shop")
    monkeypatch.setenv("FLASHBOT_SN", "TEST_SN_001")


def test_load_config_has_required_values():
    config = load_config()
    assert config.get("APP_KEY") == "test_app_key"
    assert config.get("APP_SECRET") == "test_app_secret"
    assert "PUDU_BASE_URL" in config


def test_require_config_raises_when_missing():
    with pytest.raises(EnvironmentError):
        require_config({})


def test_pudu_api_client_initialization():
    config = load_config()
    require_config(config)
    client = PuduApiClient(app_key=config["APP_KEY"], app_secret=config["APP_SECRET"])
    assert client.app_key == config["APP_KEY"]
    assert client.app_secret == config["APP_SECRET"]


def test_flashbot_controller_initialization():
    controller = FlashbotController()
    assert controller.client is not None
    assert controller.default_sn is not None

