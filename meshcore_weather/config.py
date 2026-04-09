"""Application configuration via environment variables and .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {
        "env_prefix": "MCW_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # Meshcore serial connection
    serial_port: str = "/dev/cu.usbserial-0001"
    serial_baud: int = 115200
    meshcore_channel: str = "#digitaino-wx-bot"  # Channel name or index

    # EMWIN data source
    emwin_source: str = "internet"  # "internet" or "sdr" (future)
    emwin_poll_interval: int = 120  # seconds between data refreshes
    # Initial load uses 1-hour bundle for coverage, then polls 2-minute bundle
    emwin_base_url: str = "https://tgftp.nws.noaa.gov/SL.us008001/CU.EMWIN/DF.xt/DC.gsatR/OPS/txthrs01.zip"
    emwin_poll_url: str = "https://tgftp.nws.noaa.gov/SL.us008001/CU.EMWIN/DF.xt/DC.gsatR/OPS/txtmin02.zip"
    emwin_max_age_hours: int = 12  # Expire products older than this

    # Data storage
    data_dir: Path = Path("data")

    # Logging
    log_level: str = "INFO"


settings = Settings()
