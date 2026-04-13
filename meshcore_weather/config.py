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

    # MeshWX data channel — all v4-framed broadcasts go here (empty = disabled)
    meshwx_channel: str = ""
    # Discovery channel — beacon broadcast for client auto-discovery
    meshwx_discover_channel: str = "#meshwx-discover"
    meshwx_broadcast_interval: int = 3600  # seconds between broadcasts
    meshwx_refresh_cooldown: int = 300    # min seconds between refresh per region
    meshwx_radar_grid_size: int = 32      # default grid size for on-demand radar (16, 32, or 64)

    # Coverage targeting — bot broadcasts only data affecting these areas.
    # Comma-separated lists, all optional, all additive (union). Empty = broadcast everything.
    home_cities: str = ""  # e.g. "Austin TX,San Antonio TX,Dallas TX"
    home_states: str = ""  # e.g. "TX,OK"
    home_wfos: str = ""    # e.g. "EWX,FWD,HGX"

    # Local web portal
    portal_enabled: bool = False
    portal_host: str = "0.0.0.0"
    portal_port: int = 8080

    # Admin: pubkey prefix of admin user (can run admin DM commands)
    admin_key: str = ""

    # Logging
    log_level: str = "INFO"


settings = Settings()
