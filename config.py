from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8")

    # PostgreSQL
    postgres_url: str = "postgresql://user:pass@localhost:5432/cnpj_db"

    # Paths
    base_dir: Path = Path(__file__).parent
    data_dir: Path = Path(__file__).parent / "data"
    log_dir: Path = Path(__file__).parent / "logs"
    checkpoint_dir: Path = Path(__file__).parent / "checkpoints"
    control_db: Path = Path(__file__).parent / "pipeline_control.db"
    status_file: Path = Path(__file__).parent / "ultimo_status.json"

    # Download source
    webdav_share_url: str = "https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9"
    webdav_token: str = ""
    download_timeout: int = 300
    download_chunk_size: int = 1024 * 1024  # 1 MB
    max_download_retries: int = 5

    # Pipeline
    max_job_attempts: int = 3
    lease_seconds: int = 3600
    heartbeat_interval: int = 30

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 8
    db_pool_min: int = 5
    db_pool_max: int = 20

    # Postgres schema
    pg_schema: str = "cnpj"

    # Files to download (keywords in filename)
    wanted_files: list[str] = [
        "Empresas", "Estabelecimentos", "Socios",
        "Cnaes", "Municipios", "Naturezas",
        "Qualificacoes", "Motivos", "Paises", "Portes",
    ]

    def ensure_dirs(self) -> None:
        for d in [self.data_dir, self.log_dir, self.checkpoint_dir]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
