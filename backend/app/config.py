from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    database_url: str = "postgresql+psycopg2://fastq:fastq@localhost:54384/fastq_qc"
    jwt_secret: str = "fastq-qc-pipeline-dev-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    # Simulated per-stage compute time so running jobs stay observable (and
    # can be terminated) instead of finishing in milliseconds.
    stage_delay_seconds: float = 1.5


settings = Settings()
