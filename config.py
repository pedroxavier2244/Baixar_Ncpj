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
    db_pool_min: int = 10
    db_pool_max: int = 50
    db_pool_timeout: float = 10.0   # segundos para desistir de esperar conexão do pool

    # Cache
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl: int = 2592000        # 30 dias — lookup de CNPJ individual
    cache_stale_ttl: int = 7776000  # 90 dias — fallback stale quando banco falha
    search_cache_ttl: int = 300     # 5min — resultados de busca

    # Rate limiting (requisições por minuto por IP)
    rate_limit_cnpj: str = "2000/minute"
    rate_limit_search: str = "1000/minute"

    # Segurança — API Key enviada pelo CRM no header X-API-Key
    # Deixe vazio para desabilitar (desenvolvimento)
    api_key: str = ""

    # Timeout de query no banco (ms) — evita query lenta travar o pool
    db_query_timeout_ms: int = 8000

    # Postgres schemas (banco corporativo compartilhado — nunca usar "public")
    pg_schema: str = "cnpj"                  # dados finais tratados
    pg_staging_schema: str = "cnpj_staging"  # staging UNLOGGED (COPY rápido)
    pg_serving_schema: str = "cnpj_serving"  # materialized views para API

    # Arquivos a baixar (palavras-chave no nome do arquivo)
    # Portes removido: RF parou de publicar em 2025-12 (tabela mantida no schema por compatibilidade)
    wanted_files: list[str] = [
        "Empresas", "Estabelecimentos", "Socios", "Simples",
        "Cnaes", "Municipios", "Naturezas",
        "Qualificacoes", "Motivos", "Paises",
    ]

    def ensure_dirs(self) -> None:
        for d in [self.data_dir, self.log_dir, self.checkpoint_dir]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
